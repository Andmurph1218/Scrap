"""Leitor da planilha de preços Gavazzi (CSV/XLSX)."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

CANDIDATAS_CODIGO = ("codigo", "código", "sku", "part number", "partnumber", "ref",
                     "referencia", "referência", "produto", "modelo")
CANDIDATAS_PRECO = ("preco", "preço", "price", "valor", "valor unit", "preco unitario",
                    "preço unitário", "unit price")
CANDIDATAS_DESCRICAO = ("descricao", "descrição", "description", "desc")


def _norm(texto: str) -> str:
    return re.sub(r"\s+", " ", str(texto).strip().lower())


def _achar_coluna(colunas: list[str], candidatas: tuple[str, ...],
                  override: str | None, rotulo: str) -> str | None:
    norm = {_norm(c): c for c in colunas}
    if override:
        if _norm(override) in norm:
            return norm[_norm(override)]
        raise SystemExit(f"Coluna {rotulo} '{override}' não existe. Colunas: {list(colunas)}")
    for cand in candidatas:
        for n, original in norm.items():
            if n == cand or n.startswith(cand):
                return original
    return None


def _parse_preco(valor) -> float | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    texto = re.sub(r"[^\d.,-]", "", texto)
    if not texto:
        return None
    # "1.234,56" → 1234.56 ; "1234.56" → 1234.56 ; "1234,56" → 1234.56
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif "," in texto:
        texto = texto.replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return None


def ler_planilha(caminho: str | Path, coluna_codigo: str | None = None,
                 coluna_preco: str | None = None) -> list[dict]:
    """Lê a planilha de preços e devolve [{codigo, preco, descricao_planilha?}]."""
    path = Path(caminho)
    if not path.exists():
        raise SystemExit(f"Planilha não encontrada: {path}")

    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    else:
        try:
            df = pd.read_csv(path, sep=None, engine="python", dtype=str,
                             encoding="utf-8-sig")
        except Exception:
            df = pd.read_csv(path, sep=None, engine="python", dtype=str,
                             encoding="latin-1")
    df = df.fillna("")

    col_cod = _achar_coluna(df.columns, CANDIDATAS_CODIGO, coluna_codigo, "de código")
    if not col_cod:
        raise SystemExit(
            f"Sem coluna de código na planilha. Colunas: {list(df.columns)}. "
            "Use --coluna-codigo."
        )
    col_preco = _achar_coluna(df.columns, CANDIDATAS_PRECO, coluna_preco, "de preço")
    col_desc = _achar_coluna(df.columns, CANDIDATAS_DESCRICAO, None, "")

    itens: list[dict] = []
    for _, row in df.iterrows():
        codigo = str(row[col_cod]).strip().upper()
        if not codigo:
            continue
        preco = _parse_preco(row[col_preco]) if col_preco else None
        if col_preco and preco is None:
            continue
        item = {"codigo": codigo, "preco": preco}
        if col_desc and str(row[col_desc]).strip():
            item["descricao_planilha"] = str(row[col_desc]).strip()
        itens.append(item)
    return itens
