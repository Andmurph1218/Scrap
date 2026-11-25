"""Exportador Gavazzi → CSV de importação Nuvemshop.

Reusa o template oficial e os helpers de `wago.fill_nuvemshop`. Como o CSV
da Nuvemshop não tem coluna de imagens, também grava `gavazzi-imagens.json`
({codigo: [urls]}) para uso posterior pelo módulo `photos`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from wago.config import OUT, ROOT
from wago.fill_nuvemshop import _cabecalho_template, _linha_nuvemshop, _publicar_csv
from wago.images import _slug

TEMPLATE = ROOT / "templates" / "produtos-Nuvemshop.csv"
MARCA = "Carlo Gavazzi"


def _linha(produto: dict[str, Any], header: list[str]) -> dict[str, Any]:
    codigo = str(produto.get("codigo") or "").strip()
    nome = str(produto.get("titulo_pt") or produto.get("titulo") or codigo).strip()
    descricao = str(produto.get("descricao_pt") or produto.get("descricao") or "")
    specs_pt = produto.get("specs_pt") or {}
    valores_en = produto.get("especificacoes") or {}
    if specs_pt:
        linhas = "\n".join(f"{nome_pt}: {valores_en.get(chave_en, '')}"
                           for chave_en, nome_pt in specs_pt.items()
                           if chave_en in valores_en)
        if linhas:
            descricao = f"{descricao}\n\nESPECIFICAÇÕES TÉCNICAS\n{linhas}".strip()
    preco = produto.get("preco")
    row = {
        "Identificador URL": _slug(f"{codigo}-{nome}"),
        "Nome": nome[:254],
        "SKU": codigo,
        "MPN (Cód. Exclusivo Modelo Fabricante)": codigo,
        "Marca": MARCA,
        "Descrição": descricao,
        "Preço": f"{float(preco):.2f}" if preco is not None else "",
        "Estoque": "2",
        "Exibir na loja": "SIM",
        "Produto Físico": "SIM",
        "Tags": "Gavazzi, automação industrial",
    }
    return _linha_nuvemshop(row, header)


def exportar(produtos: list[dict[str, Any]], saida: Path | str | None = None,
             *, force: bool = True) -> dict[str, Path]:
    """Gera o CSV Nuvemshop + gavazzi-imagens.json. Devolve os caminhos."""
    saida = Path(saida) if saida else OUT
    saida.mkdir(parents=True, exist_ok=True)

    header = _cabecalho_template(TEMPLATE)
    linhas = [_linha(p, header) for p in produtos]

    stamp = datetime.now().strftime("%d-%m_%H%M%S")
    csv_path = saida / f"gavazzi-nuvemshop_{stamp}.csv"
    if csv_path.exists() and not force:
        raise SystemExit(f"Arquivo já existe: {csv_path}. Use --force.")
    df = pd.DataFrame(linhas, columns=header)
    _publicar_csv(df, csv_path, header)

    imagens_path = saida / "gavazzi-imagens.json"
    mapa = {}
    if imagens_path.exists():
        try:
            mapa = json.loads(imagens_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            mapa = {}
    for p in produtos:
        mapa[str(p.get("codigo") or "")] = list(p.get("imagens") or [])
    imagens_path.write_text(
        json.dumps(mapa, ensure_ascii=False, indent=1), encoding="utf-8")

    return {"csv": csv_path, "imagens": imagens_path}
