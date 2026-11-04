"""Fase 1 — Inventário somente leitura: snapshot da loja + diagnóstico."""

from __future__ import annotations

import csv
import glob
import json
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from wago.config import (
    OUT,
    TAXONOMIA,
    TAXONOMIA_LIMITE_CONSOLIDACAO,
    TAXONOMIA_MAX_NIVEIS,
    TAXONOMIA_SNAPSHOTS,
    ensure_dirs,
)
from wago.taxonomia.nuvemshop_api import NuvemshopAPI, texto_i18n
from wago.taxonomia.texto import normalizar_nome

SNAPSHOT_VERSAO = 1

_RE_SKU = re.compile(r"\b\d{2,4}-\d{2,5}(?:/\d+)*\b")
_BREADCRUMB = {"pagina inicial", "home", "inicio", "produto", "todo", "categoria", "sem categoria"}
_INGLES = {
    "accessory", "accessorie", "connector", "block", "rail", "mounted", "tool", "marking",
    "product", "electronic", "automation", "device", "lighting", "housing", "terminal block",
    "power", "supply", "supplies", "relay", "relays", "and", "for", "with", "interface",
    "wire", "splicing", "pluggable", "board", "pcb", "module", "system", "wiring",
    "energy", "measurement", "technology", "component", "building", "ecosystem", "edge",
    "switch", "controller", "panel", "touch", "signal", "isolation", "amplifier",
}
_PALAVRAS_PT_NEUTRAS = {"interface", "terminal", "rele"}


def _skus_produto(prod: dict) -> list[str]:
    return sorted({
        str(v.get("sku")).strip() for v in prod.get("variants") or [] if v.get("sku")
    })


def _ids_categorias(prod: dict) -> list[int]:
    ids = []
    for cat in prod.get("categories") or []:
        cid = cat.get("id") if isinstance(cat, dict) else cat
        if cid is not None:
            ids.append(int(cid))
    return sorted(set(ids))


def normalizar_categoria(cat: dict) -> dict:
    parent = cat.get("parent")
    return {
        "id": int(cat["id"]),
        "nome": texto_i18n(cat.get("name")),
        "handle": texto_i18n(cat.get("handle")),
        "parent_id": int(parent) if parent not in (None, 0, "0", "") else None,
        "subcategorias": sorted(int(s) for s in cat.get("subcategories") or []),
        "bruto": cat,
    }


def normalizar_produto(prod: dict) -> dict:
    return {
        "id": int(prod["id"]),
        "nome": texto_i18n(prod.get("name")),
        "handle": texto_i18n(prod.get("handle")),
        "descricao_html": texto_i18n(prod.get("description")),
        "skus": _skus_produto(prod),
        "categorias": _ids_categorias(prod),
        "publicado": prod.get("published"),
    }


def gerar_snapshot(
    api: NuvemshopAPI, *, log: Callable[[str], None] = print
) -> dict[str, Any]:
    log("Lendo categorias da Nuvemshop...")
    categorias = [normalizar_categoria(c) for c in api.listar_categorias()]
    log(f"  {len(categorias)} categoria(s).")
    log("Lendo produtos da Nuvemshop (todas as páginas)...")
    produtos = [
        normalizar_produto(p)
        for p in api.listar_produtos(progresso=lambda n: log(f"  {n} produto(s) lidos..."))
    ]
    return {
        "versao": SNAPSHOT_VERSAO,
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "store_id": api.store_id,
        "categorias": categorias,
        "produtos": produtos,
    }


def salvar_snapshot(snapshot: dict, destino: Path | None = None) -> Path:
    ensure_dirs()
    if destino is None:
        carimbo = snapshot["gerado_em"].replace(":", "").replace("-", "")
        destino = TAXONOMIA_SNAPSHOTS / f"snapshot-{carimbo}.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(destino)
    return destino


def carregar_snapshot(caminho: Path) -> dict:
    return json.loads(Path(caminho).read_text(encoding="utf-8"))


def ultimo_snapshot() -> Path | None:
    arquivos = sorted(TAXONOMIA_SNAPSHOTS.glob("snapshot-*.json"))
    return arquivos[-1] if arquivos else None


def skus_wago(pasta: Path = OUT) -> set[str]:
    skus: set[str] = set()
    for caminho in glob.glob(str(pasta / "*master.csv")):
        try:
            with open(caminho, encoding="utf-8-sig", newline="") as fh:
                for linha in csv.DictReader(fh, delimiter=";"):
                    sku = (linha.get("SKU") or "").strip()
                    if sku:
                        skus.add(sku.upper())
        except (OSError, csv.Error):
            continue
    return skus


def motivos_nome_suspeito(nome: str) -> list[str]:
    motivos = []
    norm = normalizar_nome(nome)
    if not norm:
        return ["nome vazio"]
    if norm in _BREADCRUMB or re.fullmatch(r"\.+|…+", nome.strip()):
        motivos.append("fragmento de breadcrumb")
    if _RE_SKU.search(nome):
        motivos.append("contém código/SKU")
    palavras = set(norm.split())
    ingles = (palavras & _INGLES) - _PALAVRAS_PT_NEUTRAS
    if ingles and len(ingles) >= max(1, len(palavras) // 2):
        motivos.append("possível nome em inglês")
    if len(nome) > 60:
        motivos.append("nome muito longo")
    if re.search(r"[<>{}|\\]|&\w+;", nome):
        motivos.append("caracteres inválidos/HTML")
    return motivos


def diagnosticar(snapshot: dict, skus_fonte_wago: set[str] | None = None) -> dict[str, Any]:
    cats = {c["id"]: c for c in snapshot["categorias"]}
    produtos = snapshot["produtos"]
    filhos: dict[int, list[int]] = defaultdict(list)
    for c in cats.values():
        if c["parent_id"] is not None and c["parent_id"] in cats:
            filhos[c["parent_id"]].append(c["id"])

    orfas = [c["id"] for c in cats.values() if c["parent_id"] is not None and c["parent_id"] not in cats]

    nivel: dict[int, int] = {}
    ciclos: list[int] = []
    for cid in cats:
        visitados, atual, n = set(), cid, 1
        while cats.get(atual, {}).get("parent_id") in cats:
            if atual in visitados:
                ciclos.append(cid)
                break
            visitados.add(atual)
            atual = cats[atual]["parent_id"]
            n += 1
        nivel[cid] = n

    diretos: dict[int, int] = defaultdict(int)
    for p in produtos:
        for cid in p["categorias"]:
            diretos[cid] += 1

    def total_com_descendentes(cid: int, vistos: set[int] | None = None) -> int:
        vistos = vistos or set()
        if cid in vistos:
            return 0
        vistos.add(cid)
        return diretos.get(cid, 0) + sum(total_com_descendentes(f, vistos) for f in filhos.get(cid, []))

    def resumo(cid: int) -> dict:
        c = cats[cid]
        pai = cats.get(c["parent_id"]) if c["parent_id"] else None
        return {
            "id": cid, "nome": c["nome"], "handle": c["handle"],
            "pai": pai["nome"] if pai else None, "nivel": nivel.get(cid),
            "produtos_diretos": diretos.get(cid, 0),
            "produtos_total": total_com_descendentes(cid),
            "subcategorias": len(filhos.get(cid, [])),
        }

    vazias = [resumo(cid) for cid in cats if total_com_descendentes(cid) == 0]

    consolidacao = []
    for cid in cats:
        qtd = total_com_descendentes(cid)
        if qtd > TAXONOMIA_LIMITE_CONSOLIDACAO:
            continue
        r = resumo(cid)
        if qtd == 0:
            r["sugestao"] = "excluir"
        elif r["pai"]:
            r["sugestao"] = f"mesclar em '{r['pai']}' ou em categoria irmã"
        else:
            r["sugestao"] = "mesclar em categoria equivalente ou excluir"
        consolidacao.append(r)

    grupos: dict[str, list[int]] = defaultdict(list)
    for c in cats.values():
        grupos[normalizar_nome(c["nome"])].append(c["id"])
    duplicadas = [
        {"nome_normalizado": k, "categorias": [resumo(i) for i in sorted(v)]}
        for k, v in sorted(grupos.items()) if len(v) > 1
    ]

    suspeitas = []
    for c in cats.values():
        motivos = motivos_nome_suspeito(c["nome"])
        if motivos:
            suspeitas.append({**resumo(c["id"]), "motivos": motivos})

    excedentes_nivel = [resumo(cid) for cid, n in nivel.items() if n > TAXONOMIA_MAX_NIVEIS]

    sem_categoria, multiplas, em_nao_folha = [], [], []
    for p in produtos:
        ids = [i for i in p["categorias"] if i in cats]
        if not ids:
            sem_categoria.append({"id": p["id"], "nome": p["nome"], "skus": p["skus"]})
            continue
        ancestrais = set()
        for i in ids:
            atual = cats[i]["parent_id"]
            while atual in cats and atual not in ancestrais:
                ancestrais.add(atual)
                atual = cats[atual]["parent_id"]
        folhas = [i for i in ids if i not in ancestrais]
        if len(folhas) > 1:
            multiplas.append({"id": p["id"], "nome": p["nome"], "categorias": [cats[i]["nome"] for i in folhas]})
        if any(filhos.get(i) for i in folhas):
            em_nao_folha.append({"id": p["id"], "nome": p["nome"]})

    skus_fonte_wago = {s.upper() for s in (skus_fonte_wago or set())}
    de_wago = sum(1 for p in produtos if any(s.upper() in skus_fonte_wago for s in p["skus"]))

    return {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "snapshot_em": snapshot.get("gerado_em"),
        "store_id": snapshot.get("store_id"),
        "totais": {
            "categorias": len(cats),
            "categorias_raiz": sum(1 for c in cats.values() if c["parent_id"] is None),
            "produtos": len(produtos),
            "produtos_fonte_wago": de_wago,
            "produtos_outras_fontes": len(produtos) - de_wago,
            "categorias_vazias": len(vazias),
            "categorias_consolidacao_sugerida": len(consolidacao),
            "grupos_duplicados": len(duplicadas),
            "categorias_nome_suspeito": len(suspeitas),
            "categorias_nivel_excedente": len(excedentes_nivel),
            "categorias_orfas": len(orfas),
            "categorias_em_ciclo": len(set(ciclos)),
            "produtos_sem_categoria": len(sem_categoria),
            "produtos_multiplas_categorias_folha": len(multiplas),
            "produtos_em_categoria_nao_folha": len(em_nao_folha),
        },
        "categorias": sorted((resumo(cid) for cid in cats), key=lambda r: -r["produtos_total"]),
        "vazias": vazias,
        "consolidacao_sugerida": sorted(consolidacao, key=lambda r: (r["produtos_total"], r["nome"])),
        "duplicadas": duplicadas,
        "nomes_suspeitos": suspeitas,
        "nivel_excedente": excedentes_nivel,
        "orfas": [resumo(i) for i in orfas],
        "produtos_sem_categoria": sem_categoria,
        "produtos_multiplas_categorias_folha": multiplas,
        "produtos_em_categoria_nao_folha": em_nao_folha,
    }


def relatorio_markdown(diag: dict, limite: int = 25) -> str:
    t = diag["totais"]
    linhas = [
        f"# Diagnóstico de taxonomia — loja {diag.get('store_id')}",
        f"Snapshot: {diag.get('snapshot_em')}",
        "",
        "| Indicador | Valor |",
        "|---|---|",
    ]
    rotulos = {
        "categorias": "Categorias", "categorias_raiz": "Categorias raiz", "produtos": "Produtos",
        "produtos_fonte_wago": "Produtos com SKU da Wago", "produtos_outras_fontes": "Produtos de outras fontes",
        "categorias_vazias": "Categorias vazias",
        "categorias_consolidacao_sugerida": f"Categorias com ≤{TAXONOMIA_LIMITE_CONSOLIDACAO} produtos (consolidar)",
        "grupos_duplicados": "Grupos de categorias duplicadas",
        "categorias_nome_suspeito": "Categorias com nome suspeito",
        "categorias_nivel_excedente": f"Categorias além de {TAXONOMIA_MAX_NIVEIS} níveis",
        "categorias_orfas": "Categorias órfãs (pai inexistente)", "categorias_em_ciclo": "Categorias em ciclo",
        "produtos_sem_categoria": "Produtos sem categoria",
        "produtos_multiplas_categorias_folha": "Produtos em mais de uma categoria final",
        "produtos_em_categoria_nao_folha": "Produtos vinculados a categoria-pai",
    }
    linhas += [f"| {rotulos[k]} | {v} |" for k, v in t.items()]

    def secao(titulo: str, itens: list, fmt: Callable[[Any], str]) -> None:
        if not itens:
            return
        linhas.extend(["", f"## {titulo} ({len(itens)})"])
        linhas.extend(f"- {fmt(i)}" for i in itens[:limite])
        if len(itens) > limite:
            linhas.append(f"- … e mais {len(itens) - limite}")

    secao("Maiores categorias", diag["categorias"][:15],
          lambda c: f"{c['nome']} (id {c['id']}, nível {c['nivel']}) — {c['produtos_total']} produto(s)")
    secao("Categorias vazias", diag["vazias"],
          lambda c: f"{c['nome']} (id {c['id']}{', pai: ' + c['pai'] if c['pai'] else ''})")
    secao(f"Consolidação sugerida (≤{TAXONOMIA_LIMITE_CONSOLIDACAO} produtos — decidir no gate da Fase 2)",
          [c for c in diag.get("consolidacao_sugerida", []) if c["produtos_total"] > 0],
          lambda c: f"{c['nome']} (id {c['id']}, {c['produtos_total']} prod.) → {c['sugestao']}")
    secao("Duplicadas", diag["duplicadas"],
          lambda g: " | ".join(f"{c['nome']} (id {c['id']}, {c['produtos_total']} prod.)" for c in g["categorias"]))
    secao("Nomes suspeitos", diag["nomes_suspeitos"],
          lambda c: f"{c['nome']} (id {c['id']}) — {', '.join(c['motivos'])}")
    secao("Além do limite de níveis", diag["nivel_excedente"],
          lambda c: f"{c['nome']} (id {c['id']}, nível {c['nivel']})")
    secao("Produtos sem categoria", diag["produtos_sem_categoria"],
          lambda p: f"{p['nome']} (id {p['id']}, SKU {', '.join(p['skus']) or '-'})")
    secao("Produtos em mais de uma categoria final", diag["produtos_multiplas_categorias_folha"],
          lambda p: f"{p['nome']} → {', '.join(p['categorias'])}")
    return "\n".join(linhas) + "\n"


def executar_inventario(
    *, api: NuvemshopAPI | None = None, log: Callable[[str], None] = print,
    cancelado: Callable[[], bool] | None = None,
) -> dict[str, Path]:
    ensure_dirs()
    api = api or NuvemshopAPI.do_env(cancelado=cancelado)
    snapshot = gerar_snapshot(api, log=log)
    caminho_snap = salvar_snapshot(snapshot)
    log(f"Snapshot salvo: {caminho_snap}")
    saida = gerar_diagnostico(caminho_snap, snapshot, log=log)
    log(f"Requisições à API: {api.requisicoes}")
    return saida


def gerar_diagnostico(
    caminho_snap: Path | None = None, snapshot: dict | None = None, *,
    log: Callable[[str], None] = print,
) -> dict[str, Path]:
    """Recalcula o diagnóstico a partir de um snapshot salvo (sem chamar a API)."""
    caminho_snap = caminho_snap or ultimo_snapshot()
    if not caminho_snap:
        raise SystemExit("Nenhum snapshot encontrado. Rode `taxonomia inventario` primeiro.")
    snapshot = snapshot or carregar_snapshot(caminho_snap)
    diag = diagnosticar(snapshot, skus_wago())
    carimbo = caminho_snap.stem.replace("snapshot-", "")
    caminho_json = TAXONOMIA / f"diagnostico-{carimbo}.json"
    caminho_md = TAXONOMIA / f"diagnostico-{carimbo}.md"
    caminho_json.write_text(json.dumps(diag, ensure_ascii=False, indent=1), encoding="utf-8")
    md = relatorio_markdown(diag)
    caminho_md.write_text(md, encoding="utf-8")
    log(md)
    log(f"Diagnóstico: {caminho_md}")
    return {"snapshot": caminho_snap, "diagnostico_json": caminho_json, "diagnostico_md": caminho_md}
