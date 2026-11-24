"""HUD do Motor de Categorias: consultas (síncronas, leves) e ações em massa (fila em background).

Sobreposições gravadas pelo operador (nunca alteram o arquivo de classificação):
- decisoes.json         {produto_id: id_alvo}       → camada "humano" da próxima classificação
- arquivar.json         {produto_id: {...}}         → sugestão de arquivamento na Fase 4
- propostas_status.json {chave: {status, id_alvo}}  → propostas da IA aprovadas/rejeitadas
"""

from __future__ import annotations

import json
import threading
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from wago.taxonomia import modelo
from wago.taxonomia.classificador import (
    MOTIVO_FALTA_INFORMACAO, STATUS_AUTO, STATUS_REVISAO, TEXTO_REVISAO_CADASTRAL,
    arq_decisoes, carregar_decisoes, registro_decisao,
)
from wago.taxonomia.texto import normalizar_nome

ESTADO_IA: dict[str, Any] = {"aviso": None}
STATUS_FILTRO = ("pendente", "fora_do_ramo", "decidido", "arquivar", "falta_informacao", "todos")
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
    """produto_id → id_alvo, já resolvido pelo caminho quando o id antigo sumiu."""
    return carregar_decisoes()


def _decisoes_brutas() -> dict:
    return _ler(arq_decisoes())


def arquivar() -> dict[str, dict]:
    return _ler(arq_arquivar())


def propostas_status() -> dict[str, dict]:
    return _ler(arq_propostas_status())


# ---------------------------------------------------------------- classificação mais recente
_cache: dict[str, Any] = {"caminho": None, "mtime": 0.0, "dados": None}


def ultima_classificacao(provider: str = "nuvemshop") -> dict | None:
    padrao = "classificacao-bling-*.json" if provider == "bling" else "classificacao-[0-9]*.json"
    arquivos = sorted(_pasta().glob(padrao), key=lambda p: p.stat().st_mtime)
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
    """Aprovada + nós novos da proposta pendente (destinos válidos para decisões)."""
    aprovada = modelo.carregar_aprovada() or {"nos": []}
    proposta = modelo.carregar_proposta()
    nos = {n["id_alvo"]: n for n in aprovada["nos"]}
    nomes = {normalizar_nome(n["nome"]) for n in aprovada["nos"]}
    if proposta:
        for n in proposta["nos"]:
            # Só entra nó inédito (nem id nem nome igual ao aprovado)
            if n["id_alvo"] not in nos and normalizar_nome(n["nome"]) not in nomes:
                nos[n["id_alvo"]] = n
    return {**aprovada, "nos": list(nos.values())}


def categorias_finais() -> list[dict]:
    tax = _taxonomia_vigente()
    nos = {n["id_alvo"]: n for n in tax["nos"]}
    com_filhos = {n.get("pai") for n in nos.values() if n.get("pai")}
    saida = []
    for ident, n in nos.items():
        if ident in com_filhos or modelo.e_generica(n["nome"]):
            continue
        saida.append({
            "id": ident, "nome": n["nome"], "pai": nos.get(n.get("pai") or "", {}).get("nome"),
            "rotulo": modelo.rotulo_caminho(nos, ident),
            "nova": not n.get("nuvemshop_id"),
        })
    return sorted(saida, key=lambda c: c["rotulo"].lower())


def raizes() -> list[dict]:
    tax = _taxonomia_vigente()
    return sorted(({"id": n["id_alvo"], "nome": n["nome"]} for n in tax["nos"] if not n.get("pai")),
                  key=lambda r: r["nome"].lower())


def nos_pai() -> list[dict]:
    """Qualquer nó pode ser pai de uma categoria nova. O rótulo é o caminho inteiro."""
    tax = _taxonomia_vigente()
    por = {n["id_alvo"]: n for n in tax["nos"]}
    saida = [{"id": n["id_alvo"], "nome": n["nome"], "rotulo": modelo.rotulo_caminho(por, n["id_alvo"])}
             for n in tax["nos"]]
    return sorted(saida, key=lambda c: c["rotulo"].lower())


# ---------------------------------------------------------------- Aba 3: organização
def _item(r: dict, dec: dict, arq: dict, nomes: dict[str, str]) -> dict:
    pid = str(r.get("produto_id", ""))
    if not pid:
        return None
    sugestoes = r.get("sugestoes") or []
    decidido = dec.get(pid)
    info_arq = arq.get(pid) if isinstance(arq.get(pid), dict) else {}
    falta = info_arq.get("motivo") == MOTIVO_FALTA_INFORMACAO
    if falta:
        status = "falta_informacao"
    elif pid in arq:
        status = "arquivar"
    elif decidido:
        status = "decidido"
    elif r.get("fora_do_ramo"):
        status = "fora_do_ramo"
    elif r.get("status") == STATUS_REVISAO:
        status = "pendente"
    else:
        status = "auto"
    return {
        "id": r.get("produto_id"), "nome": r.get("nome"), "skus": r.get("skus") or [],
        "status": status, "origem": r.get("origem"), "confianca": r.get("confianca", 0),
        "fora_do_ramo": bool(r.get("fora_do_ramo")), "fora_do_ramo_motivo": r.get("fora_do_ramo_motivo"),
        "arquivar": pid in arq, "falta_informacao": falta,
        "decisao": {"id": decidido, "nome": nomes.get(decidido, decidido)} if decidido else None,
        "categoria": {"id": r.get("categoria_id"), "nome": r.get("categoria_nome")} if r.get("categoria_id") else None,
        "sugestoes": sugestoes[:3],
        "nova_categoria": (r.get("nova_categoria") or {}).get("nome"),
        "motivo": TEXTO_REVISAO_CADASTRAL if falta else (r.get("motivos_revisao") or [""])[0],
        "ia": r.get("ia"),
    }


def _base_revisao(dados: dict) -> list[dict]:
    dec, arq = decisoes(), arquivar()
    resultados = dados.get("resultados", [])
    return [r for r in resultados
            if r.get("status") == STATUS_REVISAO or r.get("fora_do_ramo")
            or str(r.get("produto_id", "")) in dec or str(r.get("produto_id", "")) in arq]


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
        if status == "todos":
            pass  # Show all items
        elif status == "pendente" and it.get("status") not in ("pendente", "fora_do_ramo"):
            continue
        elif status in ("fora_do_ramo",) and not it.get("fora_do_ramo"):
            continue
        elif status in ("decidido", "arquivar", "falta_informacao") and it.get("status") != status:
            continue
        if origem and it.get("origem") != origem:
            continue
        if not cmin <= float(it.get("confianca") or 0) <= cmax:
            continue
        sugestoes = it.get("sugestoes") or []
        if sugestao and not any(s.get("categoria_id") == sugestao for s in sugestoes):
            continue
        if busca and busca not in normalizar_nome(f"{it.get('nome', '')} {' '.join(it.get('skus', []))}"):
            continue
        saida.append(it)
    return saida


_ORDENAVEIS_PENDENTES = {
    "nome": lambda i: normalizar_nome(i.get("nome") or ""),
    "confianca": lambda i: float(i.get("confianca") or 0),
    "origem": lambda i: str(i.get("origem") or i.get("status") or ""),
}


def _ordenar_pendentes(itens: list[dict], filtros: dict[str, Any]) -> list[dict]:
    ordem = str(filtros.get("ordem") or "")
    chave = _ORDENAVEIS_PENDENTES.get(ordem.lstrip("-"))
    if chave is None:
        return itens
    return sorted(itens, key=chave, reverse=ordem.startswith("-"))


def pendentes(filtros: dict[str, Any]) -> dict[str, Any]:
    dados = ultima_classificacao()
    if not dados:
        return {"itens": [], "total": 0, "pagina": 1, "paginas": 1, "facetas": {}, "sem_classificacao": True}
    nomes = {c["id"]: c["rotulo"] for c in categorias_finais()}
    dec, arq = decisoes(), arquivar()
    todos = [item for item in (_item(r, dec, arq, nomes) for r in _base_revisao(dados)) if item is not None]
    filtrados = _ordenar_pendentes(filtrar(todos, filtros), filtros)
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
            "status": dict(Counter(i.get("status", "pendente") for i in todos)),
            "fora_do_ramo": sum(1 for i in todos if i.get("fora_do_ramo")),
            "origem": dict(Counter(i.get("origem", "") for i in todos if i.get("status") in ("pendente", "fora_do_ramo"))),
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
        itens = [item for item in (_item(r, dec, arq, nomes) for r in _base_revisao(dados)) if item is not None]
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
    por_id = {n["id_alvo"]: n for n in tax["nos"]}
    prod_nome = {str(r.get("produto_id", "")): r.get("nome") for r in dados.get("resultados", [])}
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
            "pai": p.get("pai"),
            "pai_nome": modelo.rotulo_caminho(por_id, p["pai"]) if p.get("pai") in por_id else "",
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


# ---------------------------------------------------------------- pendentes Bling (revisão + bloqueados)
def _carregar_lotes() -> list[dict]:
    """Carrega todos os lotes de execucao."""
    dir_lotes = Path('data/taxonomia/lotes')
    lotes = []
    for f in sorted(dir_lotes.glob('lote-*.json')):
        num = f.stem.replace('lote-', '')
        if num.isdigit():
            lotes.append(json.loads(f.read_text(encoding='utf-8')))
    return lotes


def _itens_revisao_bling() -> list[dict]:
    """Produtos 'aguardando_revisao' da última classificação Bling (regras, conf. baixa)."""
    dados = ultima_classificacao("bling")
    if not dados:
        return []
    itens = []
    for r in dados.get("resultados", []):
        if r.get("status") != STATUS_REVISAO:
            continue
        itens.append({
            "id": r.get("produto_id"),
            "nome": r.get("nome"),
            "skus": r.get("skus") or [],
            "status": "revisao",
            "origem": r.get("origem") or "regras",
            "confianca": r.get("confianca") or 0,
            "motivo": (r.get("motivos_revisao") or [r.get("justificativa") or ""])[0],
            "categoria_sugerida": r.get("categoria_nome"),
            "categoria_id_alvo": r.get("categoria_id"),
            "sugestoes": (r.get("sugestoes") or [])[:3],
        })
    return itens


def _itens_bloqueados_bling() -> list[dict]:
    """Produtos com erro e ignorados (variação) dos lotes de aplicação no Bling.

    Cada produto inclui:
    - id: ID do produto
    - nome: nome do produto
    - motivo_bloqueio: descricao do problema
    - status: 'erro' ou 'ignorado_variacao_invalida'
    - categoria_sugerida / categoria_id_alvo: destino previsto no plano
    """
    plano_path = Path('data/taxonomia/planos/plano-bling-real.json')
    if not plano_path.exists():
        return []
    plano = json.loads(plano_path.read_text(encoding='utf-8'))
    # O plano usa bling_id em "para"; o id_alvo v8 vem do mapeamento reverso.
    reverso = {str(bid): alvo for alvo, bid in _carregar_bling_mapping().items()}
    
    # Construir dicionario produto_id -> (categoria_id, categoria_nome)
    # O plano já traz o nome legível da categoria (ex: "Tomadas"), que é o
    # que o operador deve ver em vez do ID numérico do Bling.
    categoria_por_produto = {}
    for acao in plano.get('fases', {}).get('mover', []):
        produto_id = acao.get('produto')
        if not produto_id:
            continue
        para = acao.get('para', [])
        categoria_id = para[0] if isinstance(para, list) and para else para
        categoria_nome = acao.get('categoria_nome') or str(categoria_id or '')
        categoria_por_produto[produto_id] = {
            'id': categoria_id,
            'nome': categoria_nome,
            'id_alvo': reverso.get(str(categoria_id)),
        }
    
    lotes = _carregar_lotes()
    produtos = []
    
    for lote in lotes:
        prods = lote.get('produtos')
        if isinstance(prods, list):
            # Formato antigo - procurar por status nos objetos
            for p in prods:
                produto_id = p.get('produto')
                if not produto_id:
                    continue
                if p.get('status') == 'erro':
                    produtos.append({
                        'id': produto_id,
                        'nome': p.get('nome', ''),
                        'motivo_bloqueio': p.get('erro', ''),
                        'status': 'erro',
                        'categoria_sugerida': (categoria_por_produto.get(produto_id) or {}).get('nome'),
                        'categoria_id_alvo': (categoria_por_produto.get(produto_id) or {}).get('id_alvo'),
                    })
                elif p.get('status') == 'pulado' and 'Variedura' in p.get('erro', ''):
                    produtos.append({
                        'id': produto_id,
                        'nome': p.get('nome', ''),
                        'motivo_bloqueio': 'Revisão Manual: Variação',
                        'status': 'ignorado_variacao_invalida',
                        'categoria_sugerida': (categoria_por_produto.get(produto_id) or {}).get('nome'),
                        'categoria_id_alvo': (categoria_por_produto.get(produto_id) or {}).get('id_alvo'),
                    })
        elif isinstance(prods, dict):
            # Formato novo - chaves diretas
            # Erros (230 produtos - HTTP 400)
            for p in prods.get('erro', []):
                produto_id = p.get('produto')
                if not produto_id:
                    continue
                produtos.append({
                    'id': produto_id,
                    'nome': p.get('nome', ''),
                    'motivo_bloqueio': p.get('erro', ''),
                    'status': 'erro',
                    'categoria_sugerida': (categoria_por_produto.get(produto_id) or {}).get('nome'),
                    'categoria_id_alvo': (categoria_por_produto.get(produto_id) or {}).get('id_alvo'),
                })
            # Ignorados (2.163 produtos - variação sem variações)
            for p in prods.get('ignorado_variacao_invalida', []):
                produto_id = p.get('produto')
                if not produto_id:
                    continue
                produtos.append({
                    'id': produto_id,
                    'nome': p.get('nome', ''),
                    'motivo_bloqueio': 'Revisão Manual: Variação',
                    'status': 'ignorado_variacao_invalida',
                    'categoria_sugerida': (categoria_por_produto.get(produto_id) or {}).get('nome'),
                    'categoria_id_alvo': (categoria_por_produto.get(produto_id) or {}).get('id_alvo'),
                })

    return produtos


def _todos_bling() -> list[dict]:
    """Fila unificada do Bling: bloqueados da aplicação primeiro, depois a revisão de regras."""
    from wago.taxonomia.confirmacoes_bling import confirmados, reabertos

    atribuidos = confirmados()
    itens = {}
    for item in _itens_bloqueados_bling() + _itens_revisao_bling() + reabertos():
        pid = int(item["id"])
        if pid not in atribuidos:
            itens.setdefault(pid, item)
    return list(itens.values())


def _filtrar_bling(itens: list[dict], filtros: dict[str, Any]) -> list[dict]:
    status = filtros.get("status") or "todos"
    busca = normalizar_nome(filtros.get("busca") or "")
    try:
        cmin = float(filtros.get("conf_min") or 0)
        cmax = float(filtros.get("conf_max") if filtros.get("conf_max") not in (None, "") else 1)
    except (TypeError, ValueError) as exc:
        raise ErroTaxonomia(400, "Faixa de confiança inválida.") from exc
    saida = []
    for it in itens:
        if status not in ("todos", it.get("status")):
            continue
        if not cmin <= float(it.get("confianca") or 0) <= cmax:
            continue
        if busca and busca not in normalizar_nome(f"{it.get('nome', '')} {it.get('id', '')} {' '.join(str(s) for s in it.get('skus', []))}"):
            continue
        saida.append(it)
    return saida


def pendentes_bling(filtros: dict[str, Any] | None = None) -> dict[str, Any]:
    """Tabela unificada do Bling: revisão de regras + bloqueados, com filtro e paginação."""
    filtros = filtros or {}
    ocultar = {s for s in str(filtros.get("ocultar") or "").split(",") if s}
    base = _todos_bling()
    from wago.taxonomia.confirmacoes_bling import confirmados

    aplicados = set(confirmados())
    universo = aplicados | {int(i["id"]) for i in base}
    planos = sorted((_pasta() / "planos").glob("plano-bling*.json"), key=lambda p: p.stat().st_mtime)
    if planos:
        plano = json.loads(planos[-1].read_text(encoding="utf-8"))
        universo.update(int(a["produto"]) for a in plano.get("fases", {}).get("mover", []) if a.get("produto"))
    aplicacao = {"aplicados": len(aplicados), "restantes": len(universo - aplicados), "total": len(universo)}
    todos = [i for i in base if str(i.get("id")) not in ocultar]
    if not todos:
        return {"itens": [], "total": 0, "pagina": 1, "paginas": 1,
                "aplicacao": aplicacao,
                "facetas": {"status": {}, "origem": {}}, "total_plataforma": 0}
    filtrados = _ordenar_pendentes(_filtrar_bling(todos, filtros), filtros)
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
        "total_plataforma": len(todos),
        "aplicacao": aplicacao,
        "facetas": {
            "status": dict(Counter(i.get("status", "revisao") for i in todos)),
            "origem": dict(Counter(i.get("origem", "") for i in todos if i.get("status") == "revisao")),
        },
    }


def bling_ids_por_filtro(filtros: dict[str, Any]) -> list[int]:
    """IDs de todos os itens do filtro (sem paginação) — para ações em massa."""
    ocultar = {s for s in str(filtros.get("ocultar") or "").split(",") if s}
    todos = [i for i in _todos_bling() if str(i.get("id")) not in ocultar]
    return [int(i["id"]) for i in _filtrar_bling(todos, filtros)]


def bling_alvos_por_produto() -> dict[str, str]:
    """produto_id → id_alvo v8 sugerido (revisão + bloqueados), p/ 'aceitar sugestão'."""
    return {str(i["id"]): i["categoria_id_alvo"]
            for i in _todos_bling() if i.get("categoria_id_alvo")}


# ---------------------------------------------------------------- status
def _resumo_classificacao(dados: dict, st: dict, provider: str) -> dict:
    """Resumo do card, compatível com os formatos Nuvemshop e Bling."""
    resumo = dados.get("resumo") or {}
    resultados = dados.get("resultados") or []
    total = int(resumo.get("total") or dados.get("total_produtos") or 0)
    auto = int(resumo.get("categorizados_auto") or dados.get("classificados_auto")
               or sum(1 for r in resultados if r.get("status") == STATUS_AUTO))
    pend = int(resumo.get("aguardando_revisao") or dados.get("para_revisao")
               or sum(1 for r in resultados if r.get("status") == STATUS_REVISAO))
    por_raiz: list[dict] = []
    bloqueados = 0
    if provider == "bling":
        fila = _todos_bling()
        pend = sum(i.get("status") == "revisao" for i in fila)
        bloqueados = sum(i.get("status") != "revisao" for i in fila)
    else:
        dec, arq = decisoes(), arquivar()
        base = _base_revisao(dados)
        pend = len([r for r in base if r.get("status") == STATUS_REVISAO
                    and str(r.get("produto_id", "")) not in dec
                    and str(r.get("produto_id", "")) not in arq])
        por_raiz = _pendentes_por_raiz(base, dec)
    return {
        "arquivo": dados.get("_arquivo"), "gerada_em": dados.get("gerada_em") or dados.get("gerado_em"),
        "parcial": dados.get("parcial", False), "taxonomia_versao": dados.get("taxonomia_versao"),
        "desatualizada": False if provider == "bling"
        else dados.get("taxonomia_versao") != st.get("versao"),
        "total": total,
        "auto": auto,
        "pendentes": pend,
        "fora_do_ramo": sum(1 for r in resultados if r.get("fora_do_ramo")),
        "decididos": len(decisoes()) if provider != "bling" else 0,
        "arquivar": len(arquivar()) if provider != "bling" else 0,
        "bloqueados": bloqueados,
        "pendentes_por_raiz": por_raiz,
        "ia": {k: (dados.get("ia") or {}).get(k) for k in
               ("provedor", "modelo", "lotes_feitos", "lotes_total", "chamadas", "pausas_cota")}
        if dados.get("ia") else None,
    }


def _status_taxonomia_bling() -> dict:
    dados = _ler_bling_categorias()
    return {
        "aprovada": True,
        "versao": None,
        "aprovado_por": "Bling",
        "aprovado_em": dados.get("gerado_em"),
        "proposta_pendente": False,
        "liberado_para_classificacao": True,
        "resumo_proposta": None,
        "total_categorias": len(_taxonomia_vigente().get("nos") or []),
    }


def _carregar_bling_mapping() -> dict:
    """id_alvo da v8 → bling_id (bling_mapping.json). Vazio se ausente."""
    from wago.config import STATE
    caminho = STATE / "bling_mapping.json"
    if not caminho.exists():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError):
        return {}


def _ler_bling_categorias() -> dict:
    caminho = _pasta() / "categoria-bling-real.json"
    if not caminho.exists():
        return {}
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _bling_bloqueados() -> int:
    """Produtos que travaram na aplicação do plano Bling (erro + variação inválida)."""
    try:
        return len(_itens_bloqueados_bling())
    except (OSError, ValueError, KeyError):
        return 0


def status(execucao: dict | None, provider: str = "nuvemshop") -> dict[str, Any]:
    st = modelo.status_taxonomia() if provider != "bling" else _status_taxonomia_bling()
    dados = ultima_classificacao(provider)
    resumo_cls = _resumo_classificacao(dados, st, provider) if dados else None
    aviso = ESTADO_IA.get("aviso") if execucao else None
    return {"taxonomia": st, "classificacao": resumo_cls, "aviso_ia": aviso,
            "propostas": propostas("todas")["contagem"] if dados and provider != "bling" else {}}


def _pendentes_por_raiz(pend: list[dict], dec: dict[str, str], limite: int = 5) -> list[dict]:
    """Onde as pendências vão cair: alvo decidido/sugerido/atual → raiz da taxonomia."""
    tax = _taxonomia_vigente()
    por_id = {n["id_alvo"]: n for n in tax["nos"]}
    raiz_de: dict[str, str] = {}

    def raiz(ident: str) -> str | None:
        if ident not in raiz_de:
            cadeia = modelo.cadeia_ate_raiz(por_id, ident)
            raiz_de[ident] = cadeia[-1] if cadeia else None
        return raiz_de[ident]

    contagem: Counter[str] = Counter()
    for r in pend:
        pid = str(r.get("produto_id", ""))
        if not pid:
            continue
        alvo = dec.get(pid) or ((r.get("sugestoes") or [{}])[0].get("categoria_id")) or r.get("categoria_id")
        nome = (por_id.get(raiz(alvo) or "") or {}).get("nome") or "(sem destino)"
        contagem[nome] += 1
    return [{"nome": n, "total": t} for n, t in contagem.most_common(limite)]


_CHAVES_RESUMO_PLANO = (
    "criar", "renomear", "reparentar", "mover", "arquivar", "excluir",
    "exclusoes_bloqueadas", "produtos_nao_planejados", "total_acoes", "chamadas_api_estimadas",
)


def arvore_resumo(provider: str = "nuvemshop") -> dict[str, Any]:
    """As macro-raízes e, sob cada uma, a árvore de filhas da plataforma vigente."""
    if provider == "bling":
        # Bling usa a mesma fonte da verdade v8 (taxonomia.json); o bling_mapping
        # só acrescenta o id do Bling a cada nó (nomes e contagens vêm da v8).
        tax = _taxonomia_vigente()
        mapping = _carregar_bling_mapping()
        nos = [{**n, "bling_id": mapping.get(n["id_alvo"])} for n in tax["nos"]] if mapping else list(tax["nos"])
        versao = "Bling"
    else:
        tax = _taxonomia_vigente()
        nos = tax["nos"]
        versao = tax.get("versao")
    por_pai: dict[str | None, list[dict]] = defaultdict(list)
    for no in nos:
        por_pai[no.get("pai") or None].append(no)

    def contar(ident: str) -> int:
        return sum(1 + contar(filho["id_alvo"]) for filho in por_pai.get(ident, []))

    def montar(no: dict) -> dict[str, Any]:
        filhos = sorted(por_pai.get(no["id_alvo"], []), key=lambda n: n["nome"].lower())
        return {
            "id": no["id_alvo"], "nome": no["nome"],
            "subcategorias": contar(no["id_alvo"]),
            "bling_id": no.get("bling_id"),
            "filhos": [montar(filho) for filho in filhos],
        }

    raizes = sorted(por_pai.get(None, []), key=lambda n: n["nome"].lower())
    return {"versao": versao, "raizes": [montar(raiz) for raiz in raizes]}


def stats_classificacao(provider: str = "nuvemshop") -> dict[str, Any]:
    """Distribuição usada pela barra do card: os mesmos totais do status.

    Args:
        provider: Plataforma atual ('nuvemshop' ou 'bling').
    """
    vazio = {"total": 0, "automaticos": 0, "pendentes": 0, "decididos": 0, "fora_do_ramo": 0}
    dados = ultima_classificacao(provider)
    if not dados:
        return vazio
    resumo = dados.get("resumo") or {}
    resultados = dados.get("resultados") or []
    total = int(resumo.get("total") or dados.get("total_produtos") or 0)
    if total <= 0:
        return vazio
    return {
        "total": total,
        "automaticos": int(resumo.get("categorizados_auto") or dados.get("classificados_auto")
                           or sum(1 for r in resultados if r.get("status") == STATUS_AUTO)),
        "pendentes": int(resumo.get("aguardando_revisao") or dados.get("para_revisao")
                         or sum(1 for r in resultados if r.get("status") == STATUS_REVISAO)),
        "decididos": len(decisoes()) if provider != "bling" else 0,
        "fora_do_ramo": sum(1 for r in resultados if r.get("fora_do_ramo")),
    }


def resumo_plano(provider: str = "nuvemshop") -> dict[str, Any]:
    """Resumo do plano mais recente, sem devolver a lista de ações."""
    padrao = "plano-bling-*.json" if provider == "bling" else "plano-*.json"
    planos = sorted((_pasta() / "planos").glob(padrao), key=lambda p: p.stat().st_mtime)
    if provider != "bling":
        planos = [p for p in planos if "bling" not in p.name]
    if not planos:
        return {"existe": False}
    plano = json.loads(planos[-1].read_text(encoding="utf-8"))
    bruto = plano.get("resumo") or {}
    return {
        "existe": True,
        "arquivo": planos[-1].name,
        "gerado_em": plano.get("gerado_em"),
        "taxonomia_versao": plano.get("taxonomia_versao"),
        "resumo": {chave: int(bruto.get(chave) or 0) for chave in _CHAVES_RESUMO_PLANO},
    }


# ---------------------------------------------------------------- ações (rodam na fila)
def _destinos_validos() -> set[str]:
    return {c["id"] for c in categorias_finais()}


def _sem_revisao_cadastral(ids: list[int], log: Callable[[str], None]) -> list[int]:
    falta = {k for k, v in arquivar().items()
             if isinstance(v, dict) and v.get("motivo") == MOTIVO_FALTA_INFORMACAO}
    livres = [pid for pid in ids if str(pid) not in falta]
    if len(livres) != len(ids):
        log(f"{len(ids) - len(livres)} produto(s) em revisão cadastral ficaram sem categoria.")
    if not livres:
        raise ErroTaxonomia(400, "Produto em revisão cadastral não recebe categoria. Remova a flag antes.")
    return livres


def acao_atribuir(params: dict[str, Any], log: Callable[[str], None] = print) -> int:
    ids = resolver_ids(params)
    if not ids:
        raise ErroTaxonomia(400, "Nenhum produto selecionado.")
    ids = _sem_revisao_cadastral(ids, log)
    validos = _destinos_validos()
    bruto = _decisoes_brutas()
    if params.get("usar_sugestao"):
        dados = ultima_classificacao() or {"resultados": []}
        resultados = dados.get("resultados", [])
        sug = {r.get("produto_id"): (r.get("sugestoes") or [{}])[0].get("categoria_id") for r in resultados}
        feitos = 0
        for pid in ids:
            alvo = sug.get(pid)
            if alvo in validos:
                bruto[str(pid)] = registro_decisao(alvo)
                feitos += 1
        log(f"Sugestão principal aceita para {feitos} de {len(ids)} produto(s).")
    else:
        alvo = params.get("categoria_id")
        if alvo not in validos:
            raise ErroTaxonomia(400, "Categoria inválida (precisa ser uma categoria final da taxonomia).")
        for pid in ids:
            bruto[str(pid)] = registro_decisao(alvo)
        feitos = len(ids)
        log(f"{feitos} produto(s) atribuído(s) a '{alvo}' (decisão humana, confiança 1.0).")
    _gravar(arq_decisoes(), bruto)
    return feitos


def acao_desfazer(params: dict[str, Any], log: Callable[[str], None] = print) -> int:
    ids = {str(i) for i in resolver_ids(params)}
    bruto = _decisoes_brutas()
    removidos = [i for i in ids if bruto.pop(i, None) is not None]
    _gravar(arq_decisoes(), bruto)
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
        resultados = dados.get("resultados", [])
        motivo = {r.get("produto_id"): r.get("fora_do_ramo_motivo") for r in resultados}
        revisao_cadastral = params.get("motivo") == MOTIVO_FALTA_INFORMACAO
        for pid in ids:
            arq[str(pid)] = {
                "em": agora,
                "motivo": MOTIVO_FALTA_INFORMACAO if revisao_cadastral else (motivo.get(pid) or "marcado pelo operador"),
            }
        if revisao_cadastral:
            log(f"{len(ids)} produto(s) oculto(s) para revisão cadastral. Saíram da fila de classificação.")
        else:
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
        if pai and not any(n["id_alvo"] == pai for n in base["nos"]):
            pai = None
        try:
            nova = modelo.editar(base, "adicionar", nome=nome, pai=pai, origem_no="ia",
                                 sinonimos=[v for v in prop.get("variantes") or [] if v != nome])
        except ValueError as exc:
            raise ErroTaxonomia(400, f"Proposta não pode entrar na árvore: {exc}") from exc
        modelo.salvar_proposta(nova)
        destino = nova["nos"][-1]["id_alvo"]
        log(f"Categoria '{nome}' adicionada à taxonomia (aguardando aprovação da nova versão) sob '{pai or 'raiz'}'.")
    bruto = _decisoes_brutas()
    for pid in prop["produtos"]:
        bruto.setdefault(str(pid), registro_decisao(destino))
    _gravar(arq_decisoes(), bruto)
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
