"""Fase 4 — Plano de mudanças (diff).

A API da Nuvemshop não tem merge: cada mesclagem é expandida em ações atômicas, nesta ordem:
renomear B → reparentar filhas de A → mover produtos de A → verificar A vazia → excluir A.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

# Nuvemshop: `PUT /products/{id}` aceita `visibility` (visible|unlisted|hidden) OU `published`, nunca os
# dois (HTTP 422). `hidden` some da loja e da URL direta; `published: false` é o fallback de APIs antigas.
PAYLOAD_ARQUIVAR = {"visibility": "hidden"}
PAYLOAD_ARQUIVAR_FALLBACK = {"published": False}
VERSAO_PLANO = 1
# Marcas do ramo: arquivamento de produto delas (sem sinal de "fora do ramo") é destacado para conferência.
MARCAS_DO_RAMO = (
    "siemens", "schneider", "telemecanique", "eaton", "moeller", "weg", "finder", "wago", "kap ", "steck",
    "schmersal", "ace ", "carlo gavazzi", "merlin gerin", "conexel", "pial", "legrand", "margirius", "kacon",
    "bhs", "allen-bradley", "omron", "turk", "jng", "sindal", "metaltex", "phoenix", "abb", "ge ", "soprano",
)


def _ids_alvo_para_loja(tax: dict) -> dict[str, int | None]:
    return {n["id_alvo"]: n.get("nuvemshop_id") for n in tax.get("nos") or []}


def expandir_mesclagem(
    seq_grupo: int,
    mesclagem: dict,
    tax: dict,
    snapshot: dict,
    destino_produto: dict[int, int] | None = None,
) -> dict[str, Any] | None:
    a = mesclagem.get("origem_nuvemshop_id")
    b = mesclagem.get("destino_nuvemshop_id")
    if a is None or b is None or a == b:
        return None
    cats = {c["id"]: c for c in snapshot["categorias"]}
    nos = {n["id_alvo"]: n for n in tax.get("nos") or []}
    alvo_loja = _ids_alvo_para_loja(tax)
    mapa = tax.get("mapa_categorias_loja") or {}
    no_b = nos.get(mesclagem.get("destino")) or next(
        (n for n in nos.values() if n.get("nuvemshop_id") == b), None
    )
    b_tem_filhas = bool(no_b) and any(n.get("pai") == no_b["id_alvo"] for n in nos.values())

    acoes: list[dict] = []

    def add(acao: str, **campos: Any) -> None:
        acoes.append({"seq": len(acoes) + 1, "acao": acao, **campos})

    if no_b and b in cats and no_b["nome"] != cats[b]["nome"]:
        add("renomear", categoria=b, de=cats[b]["nome"], nome=no_b["nome"])

    for filha in sorted(c["id"] for c in cats.values() if c["parent_id"] == a):
        info = mapa.get(str(filha)) or {}
        no_filha = nos.get(info.get("id_alvo") or "")
        if no_filha and no_filha.get("nuvemshop_id") == filha:
            novo_pai = alvo_loja.get(no_filha.get("pai")) if no_filha.get("pai") else None
        else:
            novo_pai = b  # filha também será mesclada/excluída em outro grupo; sai de A antes
        add("reparentar", categoria=filha, nome=cats[filha]["nome"], de=a, para=novo_pai)

    for prod in snapshot["produtos"]:
        if a not in prod["categorias"]:
            continue
        destino = (destino_produto or {}).get(prod["id"])
        add(
            "mover_produto", produto=prod["id"], nome=prod["nome"], de=[a],
            para=[destino or b],
            requer_classificacao=destino is None and b_tem_filhas,
        )

    add("verificar_vazia", categoria=a)
    add("excluir", categoria=a, nome=cats.get(a, {}).get("nome"),
        handle=cats.get(a, {}).get("handle"), aviso_seo=True)

    return {
        "grupo": f"merge-{seq_grupo:03d}",
        "tipo": "mesclagem",
        "subtipo": mesclagem.get("tipo"),
        "motivo": mesclagem.get("motivo"),
        "sobrevivente": b,
        "sobrevivente_nome": no_b["nome"] if no_b else cats.get(b, {}).get("nome"),
        "origens": [a],
        "origem_nome": cats.get(a, {}).get("nome") or mesclagem.get("origem_nome"),
        "acoes": acoes,
    }


def acoes_renomear(tax: dict, snapshot: dict, ignorar: set[int] | None = None) -> list[dict]:
    """`PUT /categories/{id}` com novo nome: mantém ID, URL de vínculo, pai e subcategorias.

    `ignorar`: sobreviventes de mesclagem, cujo `renomear` já está dentro do grupo.
    """
    cats = {c["id"]: c for c in snapshot["categorias"]}
    acoes = []
    for no in sorted(tax.get("nos") or [], key=lambda n: n["nome"].lower()):
        cid = no.get("nuvemshop_id")
        if cid is None or cid not in cats or cid in (ignorar or set()):
            continue
        if no["nome"] != cats[cid]["nome"]:
            acoes.append({
                "acao": "renomear", "categoria": cid, "de": cats[cid]["nome"], "nome": no["nome"],
                "id_alvo": no["id_alvo"],
                "subcategorias_preservadas": sorted(
                    c["id"] for c in cats.values() if c["parent_id"] == cid
                ),
            })
    for i, acao in enumerate(acoes, 1):
        acao["seq"] = i
    return acoes


def _ref(no: dict | None) -> int | str | None:
    """ID da loja ou referência provisória `novo:<id_alvo>` (resolvida após o POST na Fase 5)."""
    if no is None:
        return None
    return no["nuvemshop_id"] if no.get("nuvemshop_id") else f"novo:{no['id_alvo']}"


def gerar_plano(
    tax: dict, snapshot: dict, classificacao: dict, *,
    decisoes: dict[str, str] | None = None, arquivar: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Diff estado atual × taxonomia aprovada × classificação. Não chama a API (simulação/dry-run).

    Ordem global obrigatória: criar → renomear → reparentar → mover → arquivar → verificar → excluir.
    """
    decisoes, arquivar = decisoes or {}, arquivar or {}
    nos = {n["id_alvo"]: n for n in tax["nos"]}
    por_loja = {n["nuvemshop_id"]: n for n in nos.values() if n.get("nuvemshop_id")}
    cats = {c["id"]: c for c in snapshot["categorias"]}
    com_filhos = {n.get("pai") for n in nos.values() if n.get("pai")}
    resultados = {r["produto_id"]: r for r in classificacao.get("resultados") or []}
    fases: dict[str, list[dict]] = {k: [] for k in
                                    ("criar", "renomear", "reparentar", "mover", "arquivar", "verificar", "excluir")}

    # 1) criar (raízes antes das filhas)
    for no in sorted((n for n in nos.values() if not n.get("nuvemshop_id")), key=lambda n: (bool(n.get("pai")), n["nome"])):
        fases["criar"].append({"acao": "criar", "ref": _ref(no), "id_alvo": no["id_alvo"], "nome": no["nome"],
                               "pai": _ref(nos.get(no.get("pai") or "")), "endpoint": "POST /categories",
                               "tratamento_422": "HTTP 422 (conflito de nome/slug): buscar a categoria "
                                                 "existente por nome/handle e reutilizar o ID"})
    # 2) renomear (mesmo ID: vínculos e subcategorias preservados)
    for a in acoes_renomear(tax, snapshot):
        a.pop("seq", None)
        fases["renomear"].append({**a, "endpoint": f"PUT /categories/{a['categoria']}"})
    # 3) reparentar (pai desejado ≠ pai atual)
    for cid, no in sorted(por_loja.items()):
        if cid not in cats:
            continue
        desejado = _ref(nos.get(no.get("pai") or ""))
        atual = cats[cid]["parent_id"]
        if desejado != atual:
            fases["reparentar"].append({"acao": "reparentar", "categoria": cid, "nome": no["nome"],
                                        "de": atual, "para": desejado, "endpoint": f"PUT /categories/{cid}"})

    # categorias da loja que saem (fora da taxonomia aprovada)
    usados = set(por_loja)
    a_excluir = {cid for cid in cats if cid not in usados}

    # 4) produtos: mover / arquivar / não planejados
    nao_planejados, sem_alteracao = [], 0
    destino_final: dict[int, list] = {}
    motivos_destino = Counter()
    for prod in snapshot["produtos"]:
        pid = prod["id"]
        atuais = sorted(prod.get("categorias") or [])
        if str(pid) in arquivar:
            info = arquivar[str(pid)] if isinstance(arquivar[str(pid)], dict) else {}
            r = resultados.get(pid) or {}
            # produto arquivado não é recategorizado, mas sai das categorias que serão excluídas
            manter = [c for c in atuais if c not in a_excluir]
            payload = {**PAYLOAD_ARQUIVAR, **({"categories": manter} if manter != atuais else {})}
            nome_min = f" {(prod.get('nome') or '').lower()} "
            revisar = not r.get("fora_do_ramo") and any(m in nome_min for m in MARCAS_DO_RAMO)
            fases["arquivar"].append({
                "acao": "arquivar_produto", "produto": pid, "nome": prod.get("nome"),
                "motivo": info.get("motivo") or "fora do ramo", "fora_do_ramo": bool(r.get("fora_do_ramo")),
                "conferir": revisar, "endpoint": f"PUT /products/{pid}",
                "payload": payload, "payload_fallback": {**PAYLOAD_ARQUIVAR_FALLBACK,
                                                        **({"categories": manter} if manter != atuais else {})},
                "estado_anterior": {"published": prod.get("publicado"), "categories": atuais},
            })
            destino_final[pid] = manter
            continue
        r = resultados.get(pid) or {}
        alvo = decisoes.get(str(pid))
        origem = "humano" if alvo else r.get("origem")
        if not alvo and r.get("status") == "categorizado_auto":
            alvo = r.get("categoria_id")
        no = nos.get(alvo or "")
        if not no or alvo in com_filhos:
            nao_planejados.append({"produto": pid, "nome": prod.get("nome"),
                                   "motivo": "aguardando revisão" if not alvo else f"destino inválido: {alvo}"})
            destino_final[pid] = atuais
            continue
        para = [_ref(no)]
        destino_final[pid] = para
        if atuais == para:
            sem_alteracao += 1
            continue
        motivos_destino[origem] += 1
        fases["mover"].append({"acao": "mover_produto", "produto": pid, "nome": prod.get("nome"), "de": atuais,
                               "para": para, "categoria_nome": no["nome"], "origem": origem,
                               "confianca": 1.0 if origem == "humano" else r.get("confianca"),
                               "endpoint": f"PUT /products/{pid}", "payload": {"categories": para}})

    # 5) verificar + excluir (categorias da loja fora da taxonomia aprovada; filhas antes dos pais)
    restantes = Counter(c for cs in destino_final.values() for c in cs if isinstance(c, int))
    filhos_loja: dict[int, list[int]] = {}
    for c in cats.values():
        if c["parent_id"] is not None:
            filhos_loja.setdefault(c["parent_id"], []).append(c["id"])
    reparentados = {a["categoria"] for a in fases["reparentar"]}
    excluir = sorted(a_excluir)

    def profundidade(cid: int) -> int:
        n, atual = 0, cats[cid]["parent_id"]
        while atual in cats and n < 10:
            n, atual = n + 1, cats[atual]["parent_id"]
        return n

    mapa = tax.get("mapa_categorias_loja") or {}
    bloqueadas = []
    for cid in sorted(excluir, key=lambda c: (-profundidade(c), c)):
        c = cats[cid]
        filhas_restantes = [f for f in filhos_loja.get(cid, []) if f in usados and f not in reparentados]
        motivo = None
        if restantes.get(cid):
            motivo = f"{restantes[cid]} produto(s) sem destino aprovado continuariam nela"
        elif filhas_restantes:
            motivo = f"{len(filhas_restantes)} subcategoria(s) continuariam nela"
        fases["verificar"].append({"acao": "verificar_vazia", "categoria": cid, "nome": c["nome"],
                                   "endpoint": f"GET /products?category_id={cid} + GET /categories/{cid}"})
        acao = {"acao": "excluir", "categoria": cid, "nome": c["nome"], "handle": c.get("handle"),
                "motivo": (mapa.get(str(cid)) or {}).get("motivo") or "fora da taxonomia aprovada",
                "aviso_seo": f"A URL /{c.get('handle') or ''}/ deixará de existir (404). Considere redirecionamento 301.",
                "endpoint": f"DELETE /categories/{cid}"}
        if motivo:
            acao.update(bloqueada=True, motivo_bloqueio=motivo)
            bloqueadas.append(acao)
        else:
            fases["excluir"].append(acao)

    ordem = ("criar", "renomear", "reparentar", "mover", "arquivar", "verificar", "excluir")
    seq = 0
    for nome in ordem:
        for a in fases[nome]:
            seq += 1
            a["seq"] = seq

    mesclagens = []
    for m in tax.get("mesclagens") or []:
        a, b = m.get("origem_nuvemshop_id"), m.get("destino_nuvemshop_id")
        if a in cats and (b in cats or b is None):
            mesclagens.append({
                "tipo": m.get("tipo"), "origem": a, "origem_nome": cats[a]["nome"], "sobrevivente": b,
                "sobrevivente_nome": (por_loja.get(b) or {}).get("nome") or (cats.get(b) or {}).get("nome"),
                "produtos_movidos": sum(1 for x in fases["mover"] if a in x["de"]),
                "excluir_origem": any(x["categoria"] == a for x in fases["excluir"]),
            })

    resumo = {
        "criar": len(fases["criar"]), "renomear": len(fases["renomear"]),
        "reparentar": len(fases["reparentar"]), "mover": len(fases["mover"]),
        "arquivar": len(fases["arquivar"]),
        "arquivar_fora_do_ramo": sum(1 for a in fases["arquivar"] if a["fora_do_ramo"]),
        "arquivar_conferir": sum(1 for a in fases["arquivar"] if a["conferir"]),
        "excluir": len(fases["excluir"]),
        "exclusoes_bloqueadas": len(bloqueadas), "mesclagens": len(mesclagens),
        "produtos_sem_alteracao": sem_alteracao, "produtos_nao_planejados": len(nao_planejados),
        "mover_por_origem": dict(motivos_destino.most_common()),
        "total_acoes": seq, "chamadas_api_estimadas": seq,
    }
    return {
        "versao_plano": VERSAO_PLANO, "modo": "dry-run",
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "store_id": snapshot.get("store_id"), "snapshot_em": snapshot.get("gerado_em"),
        "taxonomia_versao": tax.get("versao"), "classificacao": classificacao.get("_arquivo"),
        "resumo": resumo, "fases": {k: fases[k] for k in ordem},
        "exclusoes_bloqueadas": bloqueadas, "mesclagens": mesclagens, "nao_planejados": nao_planejados,
    }


def relatorio_plano(plano: dict, amostra: int = 8) -> str:
    r = plano["resumo"]
    linhas = [
        f"# Plano de mudanças — {plano['modo'].upper()} (nada foi enviado à Nuvemshop)",
        f"Loja {plano.get('store_id')} · taxonomia v{plano['taxonomia_versao']} · snapshot {plano['snapshot_em']}"
        f" · gerado {plano['gerado_em']}", "",
        "| Ação | Quantidade |", "|---|---|",
        f"| Criar categorias | {r['criar']} |", f"| Renomear categorias | {r['renomear']} |",
        f"| Reparentar categorias | {r['reparentar']} |", f"| Mover produtos | {r['mover']} |",
        f"| Arquivar produtos (visibility: hidden) | {r['arquivar']} "
        f"({r['arquivar_fora_do_ramo']} sinalizados fora do ramo · ⚠ {r['arquivar_conferir']} de marcas do ramo para conferir) |",
        f"| Excluir categorias | {r['excluir']} |",
        f"| Exclusões bloqueadas (ainda com produtos/filhas) | {r['exclusoes_bloqueadas']} |",
        f"| Grupos de mesclagem | {r['mesclagens']} |",
        f"| Produtos já corretos (sem alteração) | {r['produtos_sem_alteracao']} |",
        f"| Produtos não planejados (revisão pendente) | {r['produtos_nao_planejados']} |",
        f"| **Total de chamadas à API** | **{r['total_acoes']}** |", "",
        "Mover por origem da decisão: " + ", ".join(f"{k}: {v}" for k, v in r["mover_por_origem"].items()),
    ]
    rotulos = {"criar": "nome", "renomear": "nome", "reparentar": "nome", "mover": "nome",
               "arquivar": "nome", "excluir": "nome"}
    for fase, chave in rotulos.items():
        acoes = plano["fases"][fase]
        if not acoes:
            continue
        linhas += ["", f"## {fase.capitalize()} ({len(acoes)})"]
        for a in acoes[:amostra]:
            extra = {"renomear": lambda x: f"{x['de']} → {x['nome']}",
                     "reparentar": lambda x: f"{x['nome']}: pai {x['de']} → {x['para']}",
                     "mover": lambda x: f"{x['nome']} → {x['categoria_nome']} ({x['origem']})",
                     "criar": lambda x: f"{x['nome']} (pai {x['pai'] or 'raiz'})",
                     "arquivar": lambda x: f"{x['nome']} — {x['motivo']}",
                     "excluir": lambda x: f"{x['nome']} (id {x['categoria']}) — {x['motivo']}"}[fase](a)
            linhas.append(f"- #{a['seq']} {extra}")
        if len(acoes) > amostra:
            linhas.append(f"- … e mais {len(acoes) - amostra}")
    conferir = [a for a in plano["fases"]["arquivar"] if a.get("conferir")]
    if conferir:
        linhas += ["", f"## ⚠ Arquivamentos para conferir ({len(conferir)}) — marcas do ramo sem sinal de fora do ramo"]
        linhas += [f"- #{a['seq']} {a['nome']}" for a in conferir[:amostra * 3]]
        if len(conferir) > amostra * 3:
            linhas.append(f"- … e mais {len(conferir) - amostra * 3} (ver JSON: fases.arquivar[].conferir = true)")
    if plano["exclusoes_bloqueadas"]:
        linhas += ["", f"## Exclusões bloqueadas ({len(plano['exclusoes_bloqueadas'])})"]
        linhas += [f"- {a['nome']} (id {a['categoria']}): {a['motivo_bloqueio']}" for a in plano["exclusoes_bloqueadas"][:amostra * 2]]
    return "\n".join(linhas) + "\n"


def executar_plano_dry_run(*, log: Callable[[str], None] = print) -> dict[str, Path]:
    from wago.taxonomia import modelo
    from wago.taxonomia.classificador import carregar_decisoes
    from wago.taxonomia.inventario import carregar_snapshot, ultimo_snapshot

    tax = modelo.exigir_taxonomia_aprovada()
    pasta = modelo.TAXONOMIA
    arquivos = sorted(pasta.glob("classificacao-*.json"), key=lambda p: p.stat().st_mtime)
    if not arquivos:
        raise SystemExit("Nenhuma classificação encontrada. Rode `taxonomia classificar` primeiro.")
    classificacao = json.loads(arquivos[-1].read_text(encoding="utf-8"))
    classificacao["_arquivo"] = arquivos[-1].name
    if classificacao.get("parcial"):
        raise SystemExit("A classificação mais recente ainda está em andamento; aguarde terminar.")
    if classificacao.get("taxonomia_versao") != tax.get("versao"):
        raise SystemExit(
            f"Classificação gerada com a taxonomia v{classificacao.get('taxonomia_versao')}, "
            f"mas a aprovada é v{tax.get('versao')}. Rode `taxonomia classificar` antes do plano."
        )
    arq_arquivar = pasta / "arquivar.json"
    arquivar = json.loads(arq_arquivar.read_text(encoding="utf-8")) if arq_arquivar.exists() else {}
    plano = gerar_plano(tax, carregar_snapshot(ultimo_snapshot()), classificacao,
                        decisoes=carregar_decisoes(), arquivar=arquivar)
    destino = pasta / "planos" / f"plano-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(plano, ensure_ascii=False, indent=1), encoding="utf-8")
    md = relatorio_plano(plano)
    destino.with_suffix(".md").write_text(md, encoding="utf-8")
    log(md)
    log(f"Plano (dry-run) salvo: {destino}")
    return {"json": destino, "md": destino.with_suffix(".md")}


def grupos_mesclagem(
    tax: dict, snapshot: dict, destino_produto: dict[int, int] | None = None
) -> list[dict]:
    """Grupos de mesclagem do plano. Árvores primeiro (filhas saem antes da origem ser excluída)."""
    ordenadas = sorted(tax.get("mesclagens") or [], key=lambda m: m.get("tipo") != "arvore")
    grupos = []
    for m in ordenadas:
        g = expandir_mesclagem(len(grupos) + 1, m, tax, snapshot, destino_produto)
        if g:
            grupos.append(g)
    return grupos
