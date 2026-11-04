"""HUD do Motor de Categorias: consultas (síncronas, leves) e ações em massa (fila em background).

Sobreposições gravadas pelo operador (nunca alteram o arquivo de classificação):
- decisoes.json         {produto_id: id_alvo}       → camada "humano" da próxima classificação
- arquivar.json         {produto_id: {...}}         → sugestão de arquivamento na Fase 4
- propostas_status.json {chave: {status, id_alvo}}  → propostas da IA aprovadas/rejeitadas
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from wago.taxonomia import modelo
from wago.taxonomia.classificador import STATUS_AUTO, STATUS_REVISAO, arq_decisoes
from wago.taxonomia.texto import normalizar_nome

ESTADO_IA: dict[str, Any] = {"aviso": None}
STATUS_FILTRO = ("pendente", "fora_do_ramo", "decidido", "arquivar", "todos")
_lock = threading.Lock()


class ErroTaxonomia(Exception):
    def __init__(self, status: int, mensagem: str):
        super().__init__(mensagem)
        self.status = status
        self.mensagem = mensagem


# ---------------------------------------------------------------- arquivos de sobreposição
def _pasta() -> Path:
    return modelo.TAXONOMIA


def arq_arquivar() -> Path:
    return _pasta() / "arquivar.json"


def arq_propostas_status() -> Path:
    return _pasta() / "propostas_status.json"


def _ler(caminho: Path) -> dict:
    try:
        return json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else {}
    except (OSError, ValueError):
        return {}


def _gravar(caminho: Path, dados: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(caminho)


def decisoes() -> dict[str, str]:
    return _ler(arq_decisoes())


def arquivar() -> dict[str, dict]:
    return _ler(arq_arquivar())


def propostas_status() -> dict[str, dict]:
    return _ler(arq_propostas_status())


# ---------------------------------------------------------------- classificação mais recente
_cache: dict[str, Any] = {"caminho": None, "mtime": 0.0, "dados": None}


def ultima_classificacao() -> dict | None:
    arquivos = sorted(_pasta().glob("classificacao-*.json"), key=lambda p: p.stat().st_mtime)
    if not arquivos:
        return None
    caminho = arquivos[-1]
    mtime = caminho.stat().st_mtime
    with _lock:
        if _cache["caminho"] != caminho or _cache["mtime"] != mtime:
            try:
                dados = json.loads(caminho.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return _cache["dados"]  # arquivo sendo regravado: usa a versão anterior
            dados["_arquivo"] = caminho.name
            _cache.update(caminho=caminho, mtime=mtime, dados=dados)
        return _cache["dados"]


def _taxonomia_vigente() -> dict:
    """Aprovada + nós adicionados na proposta pendente (destinos válidos para decisões)."""
    aprovada = modelo.carregar_aprovada() or {"nos": []}
    proposta = modelo.carregar_proposta()
    nos = {n["id_alvo"]: n for n in aprovada["nos"]}
    if proposta:
        nos.update({n["id_alvo"]: n for n in proposta["nos"]})
    return {**aprovada, "nos": list(nos.values())}


def categorias_finais() -> list[dict]:
    tax = _taxonomia_vigente()
    nos = {n["id_alvo"]: n for n in tax["nos"]}
    com_filhos = {n.get("pai") for n in nos.values() if n.get("pai")}
    saida = []
    for ident, n in nos.items():
        if ident in com_filhos or modelo.e_generica(n["nome"]):
            continue
        pai = nos.get(n.get("pai") or "")
        saida.append({
            "id": ident, "nome": n["nome"], "pai": pai["nome"] if pai else None,
            "rotulo": f"{pai['nome']} › {n['nome']}" if pai else n["nome"],
            "nova": not n.get("nuvemshop_id"),
        })
    return sorted(saida, key=lambda c: c["rotulo"].lower())


def raizes() -> list[dict]:
    tax = _taxonomia_vigente()
    return sorted(({"id": n["id_alvo"], "nome": n["nome"]} for n in tax["nos"] if not n.get("pai")),
                  key=lambda r: r["nome"].lower())


# ---------------------------------------------------------------- Aba 3: organização
def _item(r: dict, dec: dict, arq: dict, nomes: dict[str, str]) -> dict:
    pid = str(r["produto_id"])
    sugestoes = r.get("sugestoes") or []
    decidido = dec.get(pid)
    if pid in arq:
        status = "arquivar"
    elif decidido:
        status = "decidido"
    elif r.get("fora_do_ramo"):
        status = "fora_do_ramo"
    elif r["status"] == STATUS_REVISAO:
        status = "pendente"
    else:
        status = "auto"
    return {
        "id": r["produto_id"], "nome": r.get("nome"), "skus": r.get("skus") or [],
        "status": status, "origem": r.get("origem"), "confianca": r.get("confianca", 0),
        "fora_do_ramo": bool(r.get("fora_do_ramo")), "fora_do_ramo_motivo": r.get("fora_do_ramo_motivo"),
        "arquivar": pid in arq,
        "decisao": {"id": decidido, "nome": nomes.get(decidido, decidido)} if decidido else None,
        "categoria": {"id": r.get("categoria_id"), "nome": r.get("categoria_nome")} if r.get("categoria_id") else None,
        "sugestoes": sugestoes[:3],
        "nova_categoria": (r.get("nova_categoria") or {}).get("nome"),
        "motivo": (r.get("motivos_revisao") or [""])[0],
        "ia": r.get("ia"),
    }


def _base_revisao(dados: dict) -> list[dict]:
    dec, arq = decisoes(), arquivar()
    return [r for r in dados["resultados"]
            if r["status"] == STATUS_REVISAO or r.get("fora_do_ramo")
            or str(r["produto_id"]) in dec or str(r["produto_id"]) in arq]


def filtrar(itens: list[dict], filtros: dict[str, Any]) -> list[dict]:
    status = filtros.get("status") or "pendente"
    origem = filtros.get("origem") or ""
    busca = normalizar_nome(filtros.get("busca") or "")
    sugestao = filtros.get("sugestao") or ""
    try:
        cmin = float(filtros.get("conf_min") or 0)
        cmax = float(filtros.get("conf_max") if filtros.get("conf_max") not in (None, "") else 1)
    except (TypeError, ValueError) as exc:
        raise ErroTaxonomia(400, "Faixa de confiança inválida.") from exc
    saida = []
    for it in itens:
        if status == "pendente" and it["status"] not in ("pendente", "fora_do_ramo"):
            continue
        if status in ("fora_do_ramo",) and not it["fora_do_ramo"]:
            continue
        if status in ("decidido", "arquivar") and it["status"] != status:
            continue
        if origem and it["origem"] != origem:
            continue
        if not cmin <= float(it["confianca"] or 0) <= cmax:
            continue
        if sugestao and not any(s["categoria_id"] == sugestao for s in it["sugestoes"]):
            continue
        if busca and busca not in normalizar_nome(f"{it['nome']} {' '.join(it['skus'])}"):
            continue
        saida.append(it)
    return saida


def pendentes(filtros: dict[str, Any]) -> dict[str, Any]:
    dados = ultima_classificacao()
    if not dados:
        return {"itens": [], "total": 0, "pagina": 1, "paginas": 1, "facetas": {}, "sem_classificacao": True}
    nomes = {c["id"]: c["rotulo"] for c in categorias_finais()}
    dec, arq = decisoes(), arquivar()
    todos = [_item(r, dec, arq, nomes) for r in _base_revisao(dados)]
    filtrados = filtrar(todos, filtros)
    try:
        por_pagina = min(max(int(filtros.get("por_pagina") or 100), 10), 500)
        pagina = max(int(filtros.get("pagina") or 1), 1)
    except (TypeError, ValueError) as exc:
        raise ErroTaxonomia(400, "Paginação inválida.") from exc
    paginas = max(-(-len(filtrados) // por_pagina), 1)
    pagina = min(pagina, paginas)
    return {
        "itens": filtrados[(pagina - 1) * por_pagina: pagina * por_pagina],
        "total": len(filtrados), "pagina": pagina, "paginas": paginas,
        "facetas": {
            "status": dict(Counter(i["status"] for i in todos)),
            "fora_do_ramo": sum(1 for i in todos if i["fora_do_ramo"]),
            "origem": dict(Counter(i["origem"] for i in todos if i["status"] in ("pendente", "fora_do_ramo"))),
        },
    }


def resolver_ids(params: dict[str, Any]) -> list[int]:
    """IDs explícitos ou "todos os filtrados" (o filtro é reaplicado na hora da execução)."""
    if params.get("filtro") is not None:
        dados = ultima_classificacao()
        if not dados:
            return []
        nomes = {c["id"]: c["rotulo"] for c in categorias_finais()}
        dec, arq = decisoes(), arquivar()
        itens = [_item(r, dec, arq, nomes) for r in _base_revisao(dados)]
        return [i["id"] for i in filtrar(itens, params["filtro"])]
    return [int(i) for i in params.get("ids") or []]


# ---------------------------------------------------------------- Aba 2: propostas
def propostas(filtro_status: str = "pendente") -> dict[str, Any]:
    dados = ultima_classificacao()
    if not dados:
        return {"propostas": [], "contagem": {}}
    lista = dados.get("propostas") or (dados.get("ia") or {}).get("propostas") or []
    tax = _taxonomia_vigente()
    por_nome: dict[str, dict] = {}
    for n in tax["nos"]:
        for nome in [n["nome"], *n.get("sinonimos", [])]:
            por_nome.setdefault(normalizar_nome(nome), n)
    fora = {normalizar_nome(t) for t in tax.get("fora_do_ramo") or []}
    raiz_nome = {n["id_alvo"]: n["nome"] for n in tax["nos"] if not n.get("pai")}
    prod_nome = {r["produto_id"]: r.get("nome") for r in dados["resultados"]}
    st, dec = propostas_status(), decisoes()
    saida, contagem = [], Counter()
    for p in lista:
        chave = p.get("chave") or normalizar_nome(p["nome"])
        restantes = [pid for pid in p["produtos"] if str(pid) not in dec]
        info = st.get(chave) or {}
        status = info.get("status") or ("resolvida" if not restantes else "pendente")
        existente = por_nome.get(chave) or por_nome.get(normalizar_nome(modelo.ajustar_nome(p["nome"])))
        contagem[status] += 1
        if filtro_status not in ("todas", status):
            continue
        saida.append({
            "chave": chave, "nome": modelo.ajustar_nome(p["nome"]), "variantes": p.get("variantes") or [p["nome"]],
            "pai": p.get("pai"), "pai_nome": raiz_nome.get(p.get("pai") or ""),
            "justificativa": p.get("justificativa"), "produtos": len(p["produtos"]),
            "produtos_pendentes": len(restantes),
            "exemplos": [prod_nome.get(pid) for pid in p["produtos"][:3]],
            "existente": {"id": existente["id_alvo"], "nome": existente["nome"]} if existente else None,
            "fora_do_ramo": chave in fora or normalizar_nome(p["nome"]) in fora,
            "generica": modelo.e_generica(p["nome"]),
            "nome_longo": len(modelo.ajustar_nome(p["nome"])) > modelo.MAX_CHARS_CATEGORIA,
            "status": status, "id_alvo": info.get("id_alvo"),
        })
    return {"propostas": saida, "contagem": dict(contagem)}


# ---------------------------------------------------------------- status
def status(execucao: dict | None) -> dict[str, Any]:
    st = modelo.status_taxonomia()
    dados = ultima_classificacao()
    resumo_cls = None
    if dados:
        dec, arq = decisoes(), arquivar()
        base = _base_revisao(dados)
        pend = [r for r in base if r["status"] == STATUS_REVISAO and str(r["produto_id"]) not in dec
                and str(r["produto_id"]) not in arq]
        resumo_cls = {
            "arquivo": dados.get("_arquivo"), "gerada_em": dados.get("gerada_em"),
            "parcial": dados.get("parcial", False), "taxonomia_versao": dados.get("taxonomia_versao"),
            "desatualizada": dados.get("taxonomia_versao") != st.get("versao"),
            "total": dados["resumo"]["total"],
            "auto": sum(1 for r in dados["resultados"] if r["status"] == STATUS_AUTO),
            "pendentes": len(pend),
            "fora_do_ramo": sum(1 for r in dados["resultados"] if r.get("fora_do_ramo")),
            "decididos": len(dec), "arquivar": len(arq),
            "ia": {k: (dados.get("ia") or {}).get(k) for k in
                   ("provedor", "modelo", "lotes_feitos", "lotes_total", "chamadas", "pausas_cota")}
            if dados.get("ia") else None,
        }
    aviso = ESTADO_IA.get("aviso") if execucao else None
    return {"taxonomia": st, "classificacao": resumo_cls, "aviso_ia": aviso,
            "propostas": propostas("todas")["contagem"] if dados else {}}


# ---------------------------------------------------------------- ações (rodam na fila)
def _destinos_validos() -> set[str]:
    return {c["id"] for c in categorias_finais()}


def acao_atribuir(params: dict[str, Any], log: Callable[[str], None] = print) -> int:
    ids = resolver_ids(params)
    if not ids:
        raise ErroTaxonomia(400, "Nenhum produto selecionado.")
    validos = _destinos_validos()
    dec = decisoes()
    if params.get("usar_sugestao"):
        dados = ultima_classificacao() or {"resultados": []}
        sug = {r["produto_id"]: (r.get("sugestoes") or [{}])[0].get("categoria_id") for r in dados["resultados"]}
        feitos = 0
        for pid in ids:
            alvo = sug.get(pid)
            if alvo in validos:
                dec[str(pid)] = alvo
                feitos += 1
        log(f"Sugestão principal aceita para {feitos} de {len(ids)} produto(s).")
    else:
        alvo = params.get("categoria_id")
        if alvo not in validos:
            raise ErroTaxonomia(400, "Categoria inválida (precisa ser uma categoria final da taxonomia).")
        for pid in ids:
            dec[str(pid)] = alvo
        feitos = len(ids)
        log(f"{feitos} produto(s) atribuído(s) a '{alvo}' (decisão humana, confiança 1.0).")
    _gravar(arq_decisoes(), dec)
    return feitos


def acao_desfazer(params: dict[str, Any], log: Callable[[str], None] = print) -> int:
    ids = {str(i) for i in resolver_ids(params)}
    dec = decisoes()
    removidos = [i for i in ids if dec.pop(i, None)]
    _gravar(arq_decisoes(), dec)
    log(f"Decisão removida de {len(removidos)} produto(s).")
    return len(removidos)


def acao_arquivar(params: dict[str, Any], log: Callable[[str], None] = print) -> int:
    ids = resolver_ids(params)
    if not ids:
        raise ErroTaxonomia(400, "Nenhum produto selecionado.")
    arq = arquivar()
    agora = datetime.now().isoformat(timespec="seconds")
    if params.get("marcar", True):
        dados = ultima_classificacao() or {"resultados": []}
        motivo = {r["produto_id"]: r.get("fora_do_ramo_motivo") for r in dados["resultados"]}
        for pid in ids:
            arq[str(pid)] = {"em": agora, "motivo": motivo.get(pid) or "marcado pelo operador"}
        log(f"{len(ids)} produto(s) marcado(s) para arquivar/despublicar na Fase 4.")
    else:
        for pid in ids:
            arq.pop(str(pid), None)
        log(f"{len(ids)} produto(s) desmarcado(s) do arquivamento.")
    _gravar(arq_arquivar(), arq)
    return len(ids)


def acao_proposta(params: dict[str, Any], log: Callable[[str], None] = print) -> int:
    chave, decisao = params.get("chave"), params.get("decisao")
    if decisao not in ("aprovar", "rejeitar"):
        raise ErroTaxonomia(400, "Decisão inválida.")
    dados = ultima_classificacao() or {}
    lista = dados.get("propostas") or (dados.get("ia") or {}).get("propostas") or []
    prop = next((p for p in lista if (p.get("chave") or normalizar_nome(p["nome"])) == chave), None)
    if not prop:
        raise ErroTaxonomia(404, "Proposta não encontrada na classificação atual.")
    st = propostas_status()
    agora = datetime.now().isoformat(timespec="seconds")
    if decisao == "rejeitar":
        st[chave] = {"status": "rejeitada", "em": agora}
        _gravar(arq_propostas_status(), st)
        log(f"Proposta '{prop['nome']}' rejeitada (não será sugerida de novo nesta revisão).")
        return 0

    destino = params.get("existente")
    if destino:
        if destino not in _destinos_validos():
            raise ErroTaxonomia(400, "Categoria existente inválida.")
        log(f"Proposta '{prop['nome']}' aprovada como categoria existente '{destino}'.")
    else:
        nome = modelo.ajustar_nome(params.get("nome") or prop["nome"])
        pai = params.get("pai") or prop.get("pai") or None
        base = modelo.base_para_edicao()
        if pai and not any(n["id_alvo"] == pai and not n.get("pai") for n in base["nos"]):
            pai = None
        try:
            nova = modelo.editar(base, "adicionar", nome=nome, pai=pai, origem_no="ia",
                                 sinonimos=[v for v in prop.get("variantes") or [] if v != nome])
        except ValueError as exc:
            raise ErroTaxonomia(400, f"Proposta não pode entrar na árvore: {exc}") from exc
        modelo.salvar_proposta(nova)
        destino = nova["nos"][-1]["id_alvo"]
        log(f"Categoria '{nome}' adicionada à taxonomia (aguardando aprovação da nova versão) sob '{pai or 'raiz'}'.")
    dec = decisoes()
    for pid in prop["produtos"]:
        dec.setdefault(str(pid), destino)
    _gravar(arq_decisoes(), dec)
    st[chave] = {"status": "aprovada", "id_alvo": destino, "em": agora}
    _gravar(arq_propostas_status(), st)
    log(f"{len(prop['produtos'])} produto(s) da proposta direcionados para '{destino}'.")
    return len(prop["produtos"])


def acao_aprovar_taxonomia(params: dict[str, Any], log: Callable[[str], None] = print) -> int:
    por = str(params.get("por") or "").strip()
    if not por:
        raise ErroTaxonomia(400, "Informe o nome de quem aprova.")
    caminho = modelo.aprovar_taxonomia(por)
    log(f"Taxonomia aprovada por {por}: {caminho.name} (v{modelo.carregar_aprovada()['versao']}).")
    return 1
