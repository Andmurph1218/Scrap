"""Fase 3 — Classificação: regras determinísticas aplicadas às respostas (IA, regras, Wago).

Regras do cliente implementadas aqui:
- Blacklist de categorias genéricas (Outros, Acessórios, Diversos, Geral): nunca recebem produto.
- Nomes de novas categorias traduzidos (dicionário Wago) e limitados a 40 caracteres.
- Confiança abaixo do limiar → Aguardando Revisão Humana.
"""

from __future__ import annotations

import csv
import glob
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from wago.config import OUT, TAXONOMIA, TAXONOMIA_LIMIAR_CONFIANCA, TAXONOMIA_MAX_NIVEIS, env
from wago.taxonomia.modelo import (
    CATEGORIAS_GENERICAS,
    MAX_CHARS_CATEGORIA,
    TRADUCOES_WAGO,
    ajustar_nome,
    caminho_estavel,
    carregar_aprovada,
    cadeia_ate_raiz,
    e_generica,
    FOLHAS_FORA_UTILIDADES,
    rotulo_caminho,
)
from wago.taxonomia.texto import normalizar_nome, sanitizar_texto

STATUS_AUTO = "categorizado_auto"
STATUS_REVISAO = "aguardando_revisao"
PROMPT_VERSAO = "classificar_v1"
_PROMPT = Path(__file__).with_name("prompts") / f"{PROMPT_VERSAO}.txt"


def limiar_confianca() -> float:
    try:
        return float(env("TAXONOMIA_LIMIAR_CONFIANCA", str(TAXONOMIA_LIMIAR_CONFIANCA)))
    except (TypeError, ValueError):
        return TAXONOMIA_LIMIAR_CONFIANCA


def montar_prompt(taxonomia_txt: str, produtos_txt: str) -> str:
    exemplos = "\n".join(f'   - "{en}" → "{pt}"' for en, pt in TRADUCOES_WAGO.items())
    return _PROMPT.read_text(encoding="utf-8").format(
        categorias_genericas=", ".join(sorted(CATEGORIAS_GENERICAS)),
        exemplos_traducao=exemplos,
        max_chars=MAX_CHARS_CATEGORIA,
        limiar=limiar_confianca(),
        taxonomia=taxonomia_txt,
        produtos=produtos_txt,
    )


def ajustar_nova_categoria(proposta: dict | None) -> tuple[dict | None, str | None]:
    """Traduz/resume o nome sugerido. Devolve (proposta ajustada | None, motivo da recusa)."""
    if not proposta or not str(proposta.get("nome") or "").strip():
        return None, None
    nome = ajustar_nome(proposta["nome"])
    if e_generica(nome):
        return None, f"nova categoria genérica recusada: {nome}"
    if len(nome) > MAX_CHARS_CATEGORIA:
        return None, (
            f"nova categoria com {len(nome)} caracteres (máx. {MAX_CHARS_CATEGORIA}); "
            "precisa ser resumida pelo operador"
        )
    ajustada = {**proposta, "nome": nome}
    if nome != proposta["nome"].strip():
        ajustada["nome_original"] = proposta["nome"]
    return ajustada, None


def aplicar_regras(resposta: dict, taxonomia: dict) -> dict[str, Any]:
    """Normaliza uma resposta {produto_id, categoria_id, confianca, justificativa, nova_categoria}."""
    nos = {n["id_alvo"]: n for n in taxonomia.get("nos") or []}
    com_filhos = {n["pai"] for n in nos.values() if n.get("pai")}
    limiar = limiar_confianca()
    try:
        confianca = min(max(float(resposta.get("confianca") or 0), 0.0), 1.0)
    except (TypeError, ValueError):
        confianca = 0.0

    resultado: dict[str, Any] = {
        "produto_id": resposta.get("produto_id"),
        "categoria_id": resposta.get("categoria_id") or None,
        "confianca": confianca,
        "justificativa": str(resposta.get("justificativa") or "")[:200],
        "origem": resposta.get("origem", "ia"),
        "motivos_revisao": [],
    }
    nova, recusa = ajustar_nova_categoria(resposta.get("nova_categoria"))
    if nova and nova.get("pai"):
        pai = nova["pai"]
        if pai not in nos:
            nova = {**nova, "pai": None}
            recusa = recusa or f"pai inexistente para nova categoria: {pai}"
        elif len(cadeia_ate_raiz(nos, pai)) >= TAXONOMIA_MAX_NIVEIS:
            nova = {**nova, "pai": None}
            recusa = recusa or (
                f"pai já está no nível {TAXONOMIA_MAX_NIVEIS}; a nova categoria criaria um "
                f"{TAXONOMIA_MAX_NIVEIS + 1}º nível"
            )
    resultado["nova_categoria"] = nova
    if recusa:
        resultado["motivos_revisao"].append(recusa)

    cat_id = resultado["categoria_id"]
    if cat_id is not None:
        no = nos.get(cat_id)
        if no is None:
            resultado["motivos_revisao"].append(f"categoria inexistente na taxonomia: {cat_id}")
        elif e_generica(no["nome"]):
            resultado["motivos_revisao"].append(f"categoria genérica bloqueada: {no['nome']}")
        elif cat_id in com_filhos:
            resultado["motivos_revisao"].append(f"categoria não é final (tem subcategorias): {no['nome']}")

    if resultado["motivos_revisao"]:
        resultado["categoria_id"] = None if cat_id not in nos or e_generica(nos[cat_id]["nome"]) else cat_id
        resultado["confianca"] = 0.0
        resultado["status"] = STATUS_REVISAO
    elif cat_id is None:
        resultado["status"] = STATUS_REVISAO
        resultado["motivos_revisao"].append("sem categoria definida")
    elif confianca < limiar:
        resultado["status"] = STATUS_REVISAO
        resultado["motivos_revisao"].append(f"confiança {confianca:.2f} abaixo de {limiar:.2f}")
    else:
        resultado["status"] = STATUS_AUTO
    return resultado


# ======================================================================
# Cascata determinística: humano → Wago → categoria atual → palavras-chave → (IA)
# ======================================================================
CONF_HUMANO = 1.0
CONF_WAGO = 0.95
CONF_LOJA = 0.90
CONF_REGRA_FORTE = 0.90
CONF_REGRA_DESEMPATE = 0.86
_STOP = {"de", "da", "do", "das", "dos", "e", "para", "com", "em", "a", "o", "p", "c", "kit", "un", "pc", "pca"}
_CORTES_DESCRICAO = re.compile(r"(?i)d[uú]vidas frequentes|perguntas frequentes|garantia|nota fiscal")


def arq_decisoes() -> Path:
    return TAXONOMIA / "decisoes.json"


def _ler_decisoes_brutas() -> dict:
    caminho = arq_decisoes()
    if not caminho.exists():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dados if isinstance(dados, dict) else {}


def id_da_decisao(valor: Any) -> str | None:
    """id_alvo gravado, no formato antigo (string) ou no registro com caminho."""
    if isinstance(valor, str) and valor:
        return valor
    if isinstance(valor, dict) and valor.get("id_alvo"):
        return str(valor["id_alvo"])
    return None


def mapa_caminhos(tax: dict | None) -> dict[str, str]:
    """caminho normalizado → id_alvo atual."""
    por = {n["id_alvo"]: n for n in (tax or {}).get("nos") or [] if n.get("id_alvo")}
    return {caminho_estavel(por, i): i for i in por if caminho_estavel(por, i)}


def registro_decisao(id_alvo: str) -> dict[str, str]:
    """Valor gravado em decisoes.json: id atual + caminho estável da folha."""
    tax = carregar_aprovada() or {}
    por = {n["id_alvo"]: n for n in tax.get("nos") or [] if n.get("id_alvo")}
    return {"id_alvo": id_alvo, "caminho": caminho_estavel(por, id_alvo) if id_alvo in por else ""}


def carregar_decisoes() -> dict[str, str]:
    """produto_id → id_alvo. Se o id sumiu da árvore, resolve pelo caminho da folha."""
    bruto = _ler_decisoes_brutas()
    tax = carregar_aprovada()
    por_caminho = mapa_caminhos(tax)
    ids = {n["id_alvo"] for n in (tax or {}).get("nos") or []}
    saida: dict[str, str] = {}
    for pid, valor in bruto.items():
        if str(pid).startswith("_"):
            continue
        ident = id_da_decisao(valor)
        caminho = valor.get("caminho") if isinstance(valor, dict) else ""
        if ident and (not ids or ident in ids):
            saida[str(pid)] = ident
        elif caminho and caminho in por_caminho:
            saida[str(pid)] = por_caminho[caminho]
        elif ident:
            saida[str(pid)] = ident
    return saida


def categorias_wago_por_sku(pasta: Path = OUT) -> dict[str, str]:
    por_sku: dict[str, str] = {}
    for caminho in sorted(glob.glob(str(pasta / "*master.csv"))):
        try:
            with open(caminho, encoding="utf-8-sig", newline="") as fh:
                for linha in csv.DictReader(fh, delimiter=";"):
                    sku = (linha.get("SKU") or "").strip().upper()
                    cat = (linha.get("Categorias") or "").split(">")[0].split(",")[0].strip()
                    if sku and cat:
                        por_sku[sku] = cat
        except (OSError, csv.Error):
            continue
    return por_sku


# grafias equivalentes de tipo de produto (após normalizar_nome)
_SINONIMOS_TOKEN = {"plug": "plugue", "contactor": "contator", "lamp": "lampada", "botoe": "botao"}


def _tokens(texto: str) -> list[str]:
    return [
        _SINONIMOS_TOKEN.get(t, t)
        for t in normalizar_nome(texto).split() if t not in _STOP and not t.isdigit()
    ]


@dataclass
class IndiceTaxonomia:
    nos: dict[str, dict]
    folhas: set[str]
    filhos: dict[str, list[str]]
    por_nome: dict[str, str]
    frases: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    @classmethod
    def criar(cls, tax: dict) -> IndiceTaxonomia:
        nos = {n["id_alvo"]: n for n in tax["nos"]}
        filhos: dict[str, list[str]] = defaultdict(list)
        for n in nos.values():
            if n.get("pai"):
                filhos[n["pai"]].append(n["id_alvo"])
        folhas = {i for i in nos if i not in filhos and not e_generica(nos[i]["nome"])}
        por_nome: dict[str, str] = {}
        frases: list[tuple[str, tuple[str, ...]]] = []
        for i, n in nos.items():
            for nome in [n["nome"], *n.get("sinonimos", [])]:
                if e_generica(nome):
                    continue
                por_nome.setdefault(normalizar_nome(nome), i)
                por_nome.setdefault(normalizar_nome(ajustar_nome(nome)), i)
                toks = tuple(_tokens(nome))
                if i in folhas and toks:
                    frases.append((i, toks))
        return cls(nos, folhas, filhos, por_nome, frases)

    def folha(self, id_alvo: str | None) -> bool:
        return id_alvo in self.folhas


def _pontuar(indice: IndiceTaxonomia, nome: str, descricao: str) -> list[tuple[float, bool, str]]:
    """[(score, frase completa no nome, id_alvo)] ordenado do melhor para o pior."""
    toks_nome = _tokens(nome)
    set_nome, primeiro = set(toks_nome), (toks_nome[0] if toks_nome else "")
    set_desc = set(_tokens(_CORTES_DESCRICAO.split(descricao)[0][:400]))
    melhores: dict[str, tuple[float, bool, tuple[str, ...]]] = {}
    for ident, frase in indice.frases:
        m_nome = sum(1 for t in frase if t in set_nome) / len(frase)
        m_desc = sum(1 for t in frase if t in set_desc) / len(frase)
        if not m_nome and not m_desc:
            continue
        completa = m_nome == 1.0
        score = max(m_nome, 0.4 * m_desc) + (0.3 if frase[0] == primeiro else 0)
        if completa:
            score += 0.01 * (len(frase) - 1)  # frase mais específica vence empate
        if score > melhores.get(ident, (0, False, ()))[0]:
            melhores[ident] = (score, completa, frase)
    return sorted(
        ((s, c, i, f) for i, (s, c, f) in melhores.items()), key=lambda x: (-x[0], x[2])
    )


def classificar_produto(
    prod: dict, tax: dict, indice: IndiceTaxonomia, *,
    decisoes: dict[str, str] | None = None, wago_por_sku: dict[str, str] | None = None,
) -> dict[str, Any]:
    nome = prod.get("nome") or ""
    descricao = sanitizar_texto(prod.get("descricao_html"))
    base = {"produto_id": prod["id"], "nome": nome, "skus": prod.get("skus", []),
            "categorias_atuais": prod.get("categorias", [])}

    def res(cat: str | None, conf: float, origem: str, just: str) -> dict:
        r = aplicar_regras(
            {"produto_id": prod["id"], "categoria_id": cat, "confianca": conf,
             "justificativa": just, "origem": origem},
            tax,
        )
        r.update(base)
        r["categoria_nome"] = indice.nos[r["categoria_id"]]["nome"] if r.get("categoria_id") in indice.nos else None
        return r

    # 1) decisão humana memorizada
    decidido = (decisoes or {}).get(str(prod["id"]))
    if decidido:
        return res(decidido, CONF_HUMANO, "humano", "decisão do operador")

    # 2) categoria da Wago (SKU no master)
    for sku in prod.get("skus", []):
        cat_wago = (wago_por_sku or {}).get(sku.upper())
        if cat_wago:
            alvo = indice.por_nome.get(normalizar_nome(cat_wago)) or indice.por_nome.get(
                normalizar_nome(ajustar_nome(cat_wago))
            )
            if indice.folha(alvo):
                return res(alvo, CONF_WAGO, "wago", f"categoria Wago: {cat_wago}")
            break

    # 3) categoria atual da loja (se continua válida e não exige reclassificação)
    mapa = tax.get("mapa_categorias_loja") or {}
    alvos_folha, alvos_pai = set(), set()
    for cid in prod.get("categorias", []):
        info = mapa.get(str(cid)) or {}
        alvo = info.get("id_alvo")
        if not alvo or info.get("reclassificar"):
            continue
        if indice.folha(alvo):
            alvos_folha.add(alvo)
        elif alvo in indice.filhos:
            alvos_pai.add(alvo)
    # pai de uma folha atual não é concorrente (produto em pai + filha)
    alvos_pai -= {indice.nos[a].get("pai") for a in alvos_folha}

    pontos = _pontuar(indice, nome, descricao)
    if len(alvos_folha) == 1 and not alvos_pai:
        alvo = next(iter(alvos_folha))
        if indice.nos[alvo].get("reavaliar"):
            # categoria dividida na nova versão (ex.: Reator Eletrônico → + Reator Convencional)
            divisoes = {i for i, n in indice.nos.items() if n.get("divisao_de") == alvo and i in indice.folhas}
            entre = [p for p in pontos if p[2] in divisoes or p[2] == alvo]
            if entre and entre[0][2] in divisoes and entre[0][1]:
                novo = entre[0][2]
                return res(novo, CONF_REGRA_FORTE, "loja+regras",
                           f"divisão de '{indice.nos[alvo]['nome']}': termos de '{indice.nos[novo]['nome']}'")
        return res(alvo, CONF_LOJA, "loja", f"categoria atual válida: {indice.nos[alvo]['nome']}")

    # 4) palavras-chave (restritas às categorias atuais quando houver)
    candidatos = set(alvos_folha) | {f for p in alvos_pai for f in indice.filhos.get(p, []) if indice.folha(f)}
    restritos = [p for p in pontos if p[2] in candidatos] if candidatos else []

    def forte(lst: list) -> bool:
        if not lst or not lst[0][1]:
            return False
        if len(lst) == 1 or lst[0][0] - lst[1][0] >= 0.25:
            return True
        # empate só com frases contidas na vencedora ("Bobina" ⊂ "Bobinas para Contatores")
        vencedora = set(lst[0][3])
        empatadas = [p for p in lst[1:] if lst[0][0] - p[0] < 0.25]
        return all(p[1] and set(p[3]) < vencedora for p in empatadas)

    # categoria atual errada não pode impedir uma correspondência global inequívoca
    lista = pontos if restritos and not forte(restritos) and forte(pontos) else (restritos or pontos)
    restritos = restritos if lista is restritos else []
    if lista:
        s1, completa, alvo, _ = lista[0]
        s2 = lista[1][0] if len(lista) > 1 else 0.0
        termos = indice.nos[alvo]["nome"]
        if forte(lista):
            conf, origem = CONF_REGRA_FORTE, "loja+regras" if restritos else "regras"
        elif completa and s1 > s2 and restritos:
            conf, origem = CONF_REGRA_DESEMPATE, "loja+regras"
        else:
            conf, origem = round(min(0.8, 0.4 + 0.3 * min(s1, 1.3)), 2), "regras"
        r = res(alvo, conf, origem, f"palavras-chave de '{termos}' no nome")
        if r["status"] == STATUS_REVISAO:
            r["sugestoes"] = [
                {"categoria_id": i, "categoria_nome": indice.nos[i]["nome"], "score": round(s, 2)}
                for s, _, i, _ in lista[:3]
            ]
        return r
    return res(None, 0.0, "regras", "nenhuma regra aplicável")


def marcar_fora_do_ramo(resultado: dict, tax: dict) -> dict:
    """Sinaliza produto fora do ramo (termos em `tax["fora_do_ramo"]`) para arquivamento opcional."""
    termos = [tuple(_tokens(t)) for t in tax.get("fora_do_ramo") or []]
    if not termos:
        return resultado
    toks = set(_tokens(resultado.get("nome") or ""))
    nova = normalizar_nome((resultado.get("nova_categoria") or {}).get("nome") or "")
    motivo = next((" ".join(t) for t in termos if t and all(x in toks for x in t)), None)
    if not motivo and nova:
        motivo = next((" ".join(t) for t in termos if t and " ".join(t) == " ".join(_tokens(nova))), None)
    if motivo:
        resultado["fora_do_ramo"] = True
        resultado["fora_do_ramo_motivo"] = motivo
    return resultado


def _marcar_folha_utilidades(
    resultado: dict, tax: dict, prod: dict, folhas: set[str], loja: dict[int, str], nomes: dict[str, str],
) -> dict:
    """Produto parado numa folha de escolar, lixeira ou limpeza sai do ramo."""
    motivo = None
    if resultado.get("categoria_id") in folhas:
        motivo = nomes.get(resultado["categoria_id"])
    if motivo is None:
        for cid in prod.get("categorias") or []:
            try:
                alvo = loja.get(int(cid))
            except (TypeError, ValueError):
                alvo = None
            if alvo:
                motivo = nomes.get(alvo)
                break
    if motivo:
        resultado["fora_do_ramo"] = True
        resultado.setdefault("fora_do_ramo_motivo", motivo)
    return resultado


def classificar_lote(
    snapshot: dict, tax: dict, *, decisoes: dict[str, str] | None = None,
    wago_por_sku: dict[str, str] | None = None, cancelado: Callable[[], bool] | None = None,
    progresso: Callable[[int, int], None] | None = None,
) -> list[dict]:
    indice = IndiceTaxonomia.criar(tax)
    folhas_fora = {
        n["id_alvo"] for n in tax["nos"]
        if normalizar_nome(n["nome"]) in {normalizar_nome(x) for x in FOLHAS_FORA_UTILIDADES}
    }
    nomes_fora = {n["id_alvo"]: n["nome"] for n in tax["nos"] if n["id_alvo"] in folhas_fora}
    loja_fora: dict[int, str] = {}
    for cid, info in (tax.get("mapa_categorias_loja") or {}).items():
        if info.get("id_alvo") in folhas_fora:
            try:
                loja_fora[int(cid)] = info["id_alvo"]
            except (TypeError, ValueError):
                continue
    for n in tax["nos"]:
        if n.get("nuvemshop_id") and n["id_alvo"] in folhas_fora:
            loja_fora[int(n["nuvemshop_id"])] = n["id_alvo"]
    resultados = []
    total = len(snapshot["produtos"])
    for n, prod in enumerate(snapshot["produtos"], 1):
        if cancelado and cancelado():
            break
        resultado = marcar_fora_do_ramo(classificar_produto(
            prod, tax, indice, decisoes=decisoes, wago_por_sku=wago_por_sku,
        ), tax)
        resultados.append(_marcar_folha_utilidades(resultado, tax, prod, folhas_fora, loja_fora, nomes_fora))
        if progresso and (n % 1000 == 0 or n == total):
            progresso(n, total)
    return resultados


def resumir(resultados: list[dict]) -> dict[str, Any]:
    status = Counter(r["status"] for r in resultados)
    auto = [r for r in resultados if r["status"] == STATUS_AUTO]
    revisao = [r for r in resultados if r["status"] == STATUS_REVISAO]
    return {
        "total": len(resultados),
        "categorizados_auto": status[STATUS_AUTO],
        "aguardando_revisao": status[STATUS_REVISAO],
        "pct_auto": round(100 * status[STATUS_AUTO] / max(len(resultados), 1), 1),
        "auto_por_origem": dict(Counter(r["origem"] for r in auto).most_common()),
        "revisao_por_motivo": dict(Counter(
            re.sub(r"\s*[\d.]+\s*", " ", r["motivos_revisao"][0].split(":")[0]).strip()
            if r["motivos_revisao"] else "?"
            for r in revisao
        ).most_common()),
        "revisao_por_faixa_confianca": dict(sorted(Counter(
            f"{int(r['confianca'] * 10) / 10:.1f}" for r in revisao
        ).items())),
        "revisao_com_sugestao": sum(1 for r in revisao if r.get("categoria_id") or r.get("sugestoes")),
        "fora_do_ramo": sum(1 for r in resultados if r.get("fora_do_ramo")),
        "top_categorias_auto": Counter(r["categoria_nome"] for r in auto).most_common(20),
    }


def relatorio_classificacao(resumo: dict, resultados: list[dict], amostra: int = 20,
                            ia: dict | None = None) -> str:
    linhas = [
        f"# Classificação ({'com IA: ' + ia['provedor'] + '/' + ia['modelo'] if ia else 'sem IA'})", "",
        "| Indicador | Valor |", "|---|---|",
        f"| Produtos | {resumo['total']} |",
        f"| Categorizados automaticamente (≥ {limiar_confianca():.2f}) | {resumo['categorizados_auto']} ({resumo['pct_auto']}%) |",
        f"| Aguardando revisão humana | {resumo['aguardando_revisao']} |",
        f"| Revisão com sugestão de categoria | {resumo['revisao_com_sugestao']} |",
        f"| Fora do ramo (arquivamento opcional) | {resumo.get('fora_do_ramo', 0)} |",
        "", "## Automáticos por origem",
        *(f"- {k}: {v}" for k, v in resumo["auto_por_origem"].items()),
        "", "## Revisão por motivo",
        *(f"- {k}: {v}" for k, v in resumo["revisao_por_motivo"].items()),
        "", "## Revisão por faixa de confiança",
        *(f"- {k}: {v}" for k, v in resumo["revisao_por_faixa_confianca"].items()),
        "", "## Maiores categorias (automático)",
        *(f"- {k}: {v}" for k, v in resumo["top_categorias_auto"]),
    ]
    if ia:
        linhas += [
            "", "## Uso da IA",
            f"- Produtos enviados: {ia['enviados']} (cache: {ia['do_cache']}) · chamadas: {ia['chamadas']}",
            f"- Tokens: {ia['tokens_entrada']} entrada / {ia['tokens_saida']} saída · respostas inválidas: {ia['respostas_invalidas']}",
            f"- Pausas por limite do plano: {ia['pausas_cota']} ({round(ia['segundos_em_pausa'] / 60)} min)",
            f"- Novas categorias propostas pela IA: {len(ia.get('propostas') or [])}",
        ]
        if ia.get("interrompido"):
            linhas.append(f"- ⚠ Interrompido: {ia['interrompido']} — rode de novo para continuar (cache preserva o feito).")
    linhas += ["", f"## Amostra da revisão ({amostra})"]
    for r in [r for r in resultados if r["status"] == STATUS_REVISAO][:amostra]:
        sug = ", ".join(f"{s['categoria_nome']} ({s['score']})" for s in r.get("sugestoes", [])) or "-"
        linhas.append(f"- {r['nome']} → sugestões: {sug}")
    return "\n".join(linhas) + "\n"


def simular_proposta(tax: dict, snapshot: dict, *, log: Callable[[str], None] = print) -> dict:
    """Estimativa (só regras, sem IA) de produtos por nó da proposta, para o gate de aprovação."""
    from wago.taxonomia.modelo import alertas_no

    resultados = classificar_lote(snapshot, tax, decisoes=carregar_decisoes(),
                                  wago_por_sku=categorias_wago_por_sku())
    base = Counter(r["categoria_id"] for r in resultados if r["status"] == STATUS_AUTO and r.get("categoria_id"))
    pais = {n["id_alvo"]: n.get("pai") for n in tax["nos"]}
    contagem = Counter(base)
    for cat, qtd in base.items():
        atual = pais.get(cat)
        vistos = {cat}
        while atual and atual not in vistos:
            vistos.add(atual)
            contagem[atual] += qtd
            atual = pais.get(atual)
    por_id = {n["id_alvo"]: n for n in tax["nos"]}
    por_raiz: dict[str, int] = {}
    for ident, qtd in base.items():
        cadeia = list(reversed(cadeia_ate_raiz(por_id, ident)))
        if not cadeia:
            continue
        raiz = por_id[cadeia[0]]["nome"]
        por_raiz[raiz] = por_raiz.get(raiz, 0) + qtd
    nos = [{**n, "produtos_simulados": contagem.get(n["id_alvo"], 0)} for n in tax["nos"]]
    for n in nos:
        n["alertas"] = alertas_no(n, nos)
    resumo = resumir(resultados)
    log(f"Simulação (sem IA): {resumo['categorizados_auto']} auto ({resumo['pct_auto']}%), "
        f"{resumo['aguardando_revisao']} revisão, {resumo['fora_do_ramo']} fora do ramo.")
    sem_categoria = sum(1 for p in snapshot["produtos"] if not p.get("categorias"))
    sem_destino = sum(
        1 for r in resultados
        if r["status"] != STATUS_AUTO and not r.get("categoria_id") and not r.get("sugestoes")
    )
    return {**tax, "nos": nos, "simulacao": {
        "em": datetime.now().isoformat(timespec="seconds"), "sem_ia": True,
        **{k: resumo[k] for k in ("total", "categorizados_auto", "aguardando_revisao", "pct_auto", "fora_do_ramo")},
        "sem_categoria_na_loja": sem_categoria,
        "sem_destino": sem_destino,
        "por_raiz": dict(sorted(por_raiz.items(), key=lambda kv: -kv[1])),
    }}


# ---------------------------------------------------------------- IA (camada 4)
def arq_cache_ia() -> Path:
    return TAXONOMIA / "cache_ia.json"


def _carregar_cache() -> dict[str, dict]:
    caminho = arq_cache_ia()
    try:
        return json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else {}
    except (OSError, ValueError):
        return {}


def _salvar_cache(cache: dict[str, dict]) -> None:
    caminho = arq_cache_ia()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(caminho)


def _chave_cache(prod: dict, texto: str, tax: dict, modelo: str) -> str:
    import hashlib

    base = "|".join([PROMPT_VERSAO, str(tax.get("versao")), modelo, str(prod["id"]), prod.get("nome") or "", texto])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


def taxonomia_para_prompt(indice: IndiceTaxonomia) -> str:
    linhas = []
    for ident in sorted(indice.folhas, key=lambda i: (indice.nos[i].get("pai") or "", indice.nos[i]["nome"])):
        linhas.append(f"- {ident}: {rotulo_caminho(indice.nos, ident, ' > ')}")
    pais = sorted(indice.nos, key=lambda i: rotulo_caminho(indice.nos, i, " > "))
    linhas.append(
        "\nNós que podem ser pai de uma nova categoria (`nova_categoria.pai`), até o nível 4: "
        + ", ".join(f"{i} ({rotulo_caminho(indice.nos, i, ' > ')})" for i in pais)
    )
    linhas.append(
        "Sensores entram sob Automação e Controle. Fixação, parafusos, abraçadeiras e manutenção "
        "entram sob Fixação e Utilidades. Não proponha Outros."
    )
    return "\n".join(linhas)


def _aplicar_resposta_ia(r: dict, item: dict, tax: dict, indice: IndiceTaxonomia) -> None:
    novo = aplicar_regras({**item, "produto_id": r["produto_id"], "origem": "ia"}, tax)
    r["ia"] = {"categoria_id": item.get("categoria_id"), "confianca": item.get("confianca"),
               "justificativa": str(item.get("justificativa") or "")[:200]}
    if novo["status"] == STATUS_AUTO:
        r.update({k: novo[k] for k in ("categoria_id", "confianca", "justificativa", "origem", "status")})
        r["motivos_revisao"] = []
        r["categoria_nome"] = indice.nos[novo["categoria_id"]]["nome"]
        r.pop("sugestoes", None)
    else:
        sug = r.setdefault("sugestoes", [])
        if novo.get("categoria_id") in indice.nos and all(s["categoria_id"] != novo["categoria_id"] for s in sug):
            sug.insert(0, {"categoria_id": novo["categoria_id"], "categoria_nome": indice.nos[novo["categoria_id"]]["nome"],
                           "score": round(float(item.get("confianca") or 0), 2), "origem": "ia"})
        if novo.get("nova_categoria"):
            r["nova_categoria"] = novo["nova_categoria"]
    marcar_fora_do_ramo(r, tax)


def agrupar_propostas(resultados: list[dict]) -> list[dict]:
    """Novas categorias sugeridas pela IA, agrupadas por nome normalizado (só produtos ainda em revisão)."""
    propostas: dict[str, dict] = {}
    for r in resultados:
        nova = r.get("nova_categoria")
        if r.get("status") != STATUS_REVISAO or not nova:
            continue
        chave = normalizar_nome(nova["nome"])
        prop = propostas.setdefault(chave, {**nova, "chave": chave, "produtos": [], "variantes": []})
        prop["produtos"].append(r["produto_id"])
        if nova["nome"] not in prop["variantes"]:
            prop["variantes"].append(nova["nome"])
    return sorted(propostas.values(), key=lambda p: (-len(p["produtos"]), p["nome"]))


def classificar_com_ia(
    resultados: list[dict], snapshot: dict, tax: dict, controle: Any, *,
    lote: int = 25, limite: int | None = None, log: Callable[[str], None] = print,
    ao_lote: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """Envia à IA só os produtos em revisão (exceto decisões humanas). Cache salvo a cada lote.

    `ao_lote(info)` é chamado após cada lote já aplicado em `resultados` (atualização em tempo real).
    """
    from wago.taxonomia.ia import CanceladoIA, ErroIA

    indice = IndiceTaxonomia.criar(tax)
    provedor = controle.provedor
    prods = {p["id"]: p for p in snapshot["produtos"]}
    pendentes = [r for r in resultados if r["status"] == STATUS_REVISAO and r.get("origem") != "humano"]
    if limite:
        pendentes = pendentes[:limite]
    por_id = {r["produto_id"]: r for r in pendentes}
    cache = _carregar_cache()
    tax_txt = taxonomia_para_prompt(indice)
    info: dict[str, Any] = {"provedor": provedor.nome, "modelo": provedor.modelo, "enviados": 0,
                            "do_cache": 0, "interrompido": None, "propostas": [], "lotes_total": 0,
                            "lotes_feitos": 0}

    textos: dict[int, tuple[str, str]] = {}
    a_enviar = []
    for r in pendentes:
        prod = prods[r["produto_id"]]
        desc = _CORTES_DESCRICAO.split(sanitizar_texto(prod.get("descricao_html")))[0][:300]
        chave = _chave_cache(prod, desc, tax, provedor.modelo)
        textos[prod["id"]] = (chave, desc)
        if chave in cache:
            info["do_cache"] += 1
            _aplicar_resposta_ia(r, cache[chave], tax, indice)
        else:
            a_enviar.append(prod)
    info["lotes_total"] = -(-len(a_enviar) // lote)
    log(f"IA ({provedor.nome}/{provedor.modelo}): {len(pendentes)} produto(s) em revisão; "
        f"{info['do_cache']} já no cache, {len(a_enviar)} a enviar em lotes de {lote}.")
    if ao_lote:
        ao_lote(info)

    for i in range(0, len(a_enviar), lote):
        bloco = a_enviar[i:i + lote]
        prods_txt = "\n".join(
            f"- produto_id {p['id']}: {p.get('nome') or ''}"
            + (f" | {textos[p['id']][1]}" if textos[p["id"]][1] else "")
            for p in bloco
        )
        try:
            dados = controle.chamar(montar_prompt(tax_txt, prods_txt))
        except CanceladoIA as exc:
            info["interrompido"] = str(exc)
            break
        except ErroIA as exc:
            log(f"  lote {i // lote + 1}: {exc} — produtos seguem para revisão humana")
            continue
        recebidos = {}
        for item in dados.get("resultados") or []:
            try:
                recebidos[int(item.get("produto_id"))] = item
            except (TypeError, ValueError):
                continue
        for p in bloco:
            item = recebidos.get(p["id"])
            if item:
                cache[textos[p["id"]][0]] = item
                _aplicar_resposta_ia(por_id[p["id"]], item, tax, indice)
        info["enviados"] += len(bloco)
        info["lotes_feitos"] += 1
        _salvar_cache(cache)
        validas = sum(1 for p in bloco if p["id"] in recebidos)
        log(f"  lote {i // lote + 1}/{info['lotes_total']}: {validas}/{len(bloco)} respostas")
        if ao_lote:
            ao_lote(info)

    info["propostas"] = agrupar_propostas(resultados)
    uso = provedor.uso
    pausa = getattr(controle, "uso_pausa", None)
    info.update(chamadas=uso.chamadas, tokens_entrada=uso.tokens_entrada, tokens_saida=uso.tokens_saida,
                respostas_invalidas=uso.respostas_invalidas,
                pausas_cota=pausa.pausas_cota if pausa else 0,
                segundos_em_pausa=pausa.segundos_em_pausa if pausa else 0)
    return info


def sincronizar_arquivar(resultados: list[dict]) -> int:
    """Grava em arquivar.json quem a classificação marcou como fora do ramo.

    Marcas feitas à mão pelo operador (motivo "marcado pelo operador") são mantidas.
    """
    caminho = TAXONOMIA / "arquivar.json"
    atual: dict = {}
    if caminho.exists():
        try:
            bruto = json.loads(caminho.read_text(encoding="utf-8"))
            if isinstance(bruto, dict):
                atual = bruto
        except (OSError, ValueError):
            atual = {}
    agora = datetime.now().isoformat(timespec="seconds")
    novo = {
        k: v for k, v in atual.items()
        if isinstance(v, dict) and v.get("motivo") == "marcado pelo operador"
    }
    n = 0
    for r in resultados:
        if not r.get("fora_do_ramo"):
            continue
        novo[str(r["produto_id"])] = {"em": agora, "motivo": r.get("fora_do_ramo_motivo") or "fora do ramo"}
        n += 1
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(novo, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(caminho)
    return n


def executar_classificacao(*, sem_ia: bool = True, log: Callable[[str], None] = print,
                           cancelado: Callable[[], bool] | None = None,
                           aviso_cota: Callable[[dict], None] | None = None) -> dict[str, Path]:
    from wago.taxonomia.ia import ControleCota, obter_provedor
    from wago.taxonomia.inventario import carregar_snapshot, ultimo_snapshot
    from wago.taxonomia.modelo import exigir_taxonomia_aprovada

    tax = exigir_taxonomia_aprovada()
    caminho_snap = ultimo_snapshot()
    if not caminho_snap:
        raise SystemExit("Nenhum snapshot encontrado. Rode `taxonomia inventario` primeiro.")
    provedor = None if sem_ia else obter_provedor()
    if not sem_ia and provedor is None:
        log("Aviso: nenhuma chave de IA configurada no .env; executando somente regras.")
    snapshot = carregar_snapshot(caminho_snap)
    log(f"Classificando {len(snapshot['produtos'])} produtos com a taxonomia v{tax.get('versao')}...")
    resultados = classificar_lote(
        snapshot, tax, decisoes=carregar_decisoes(), wago_por_sku=categorias_wago_por_sku(),
        cancelado=cancelado, progresso=lambda n, t: log(f"  {n}/{t}"),
    )
    if cancelado and cancelado():
        log("Classificação cancelada antes de gravar; resultado anterior preservado.")
        return {}
    carimbo = datetime.now().strftime("%Y%m%dT%H%M%S")
    caminho_json = TAXONOMIA / f"classificacao-{carimbo}.json"
    caminho_md = caminho_json.with_suffix(".md")

    def gravar(ia_info: dict | None, parcial: bool) -> dict:
        resumo = resumir(resultados)
        saida = {
            "gerada_em": datetime.now().isoformat(timespec="seconds"),
            "taxonomia_versao": tax.get("versao"), "snapshot": caminho_snap.name,
            "parcial": parcial, "ia": ia_info, "resumo": resumo,
            "propostas": agrupar_propostas(resultados), "resultados": resultados,
        }
        tmp = caminho_json.with_suffix(".tmp")
        tmp.write_text(json.dumps(saida, ensure_ascii=False), encoding="utf-8")
        tmp.replace(caminho_json)
        return resumo

    gravar(None, parcial=bool(provedor))  # regras já visíveis no dashboard antes da IA
    ia_info = None
    if provedor:
        controle = ControleCota(provedor, log=log, aviso=aviso_cota, cancelado=cancelado,
                                rpm=float(env("IA_RPM", "10") or 10))

        def ao_lote(info: dict) -> None:
            if info["lotes_feitos"] % 3 == 0 or info["lotes_feitos"] == info["lotes_total"]:
                gravar({**info, "provedor": provedor.nome}, parcial=True)

        ia_info = classificar_com_ia(
            resultados, snapshot, tax, controle, log=log,
            lote=int(env("IA_LOTE", "25") or 25),
            limite=int(env("IA_MAX_PRODUTOS_POR_EXECUCAO", "0") or 0) or None,
            ao_lote=ao_lote,
        )
    resumo = gravar(ia_info, parcial=False)
    marcados = sincronizar_arquivar(resultados)
    log(f"Arquivamento: {marcados} produto(s) fora do ramo marcados para a Fase 4.")
    md = relatorio_classificacao(resumo, resultados, ia=ia_info)
    caminho_md.write_text(md, encoding="utf-8")
    log(md)
    log(f"Resultado: {caminho_json}")
    return {"json": caminho_json, "md": caminho_md}
