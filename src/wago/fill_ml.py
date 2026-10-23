"""Etapa 5: preenche o Anunciar-ML.xlsx oficial com dados do scrape."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell

from wago.config import OUT, ROOT, ensure_dirs


def _set_cell(ws, row: int, col: int, value: Any) -> None:
    cell = ws.cell(row, col)
    if isinstance(cell, MergedCell):
        return
    cell.value = value

TEMPLATE = ROOT / "templates" / "Anunciar-ML.xlsx"

# Headers canónicos (prefixo) → chave lógica
HEADER_ALIASES: list[tuple[str, str]] = [
    ("título:", "titulo"),
    ("titulo:", "titulo"),
    ("condição", "condicao"),
    ("condicao", "condicao"),
    ("código universal", "gtin"),
    ("codigo universal", "gtin"),
    ("fotos", "fotos"),
    ("sku", "sku"),
    ("estoque", "estoque"),
    ("preço [r$]", "preco"),
    ("preco [r$]", "preco"),
    ("descrição", "descricao"),
    ("descricao", "descricao"),
    ("largura (cm)", "largura"),
    ("altura (cm)", "altura"),
    ("profundidade (cm)", "profundidade"),
    ("peso físico", "peso"),
    ("peso fisico", "peso"),
    ("tipo de anúncio", "tipo_anuncio"),
    ("tipo de anuncio", "tipo_anuncio"),
    ("forma de envio", "forma_envio"),
    ("custo de envio", "custo_envio"),
    ("retirar pessoalmente", "retirar"),
    ("tipo de garantia", "tipo_garantia"),
    ("tempo de garantia", "tempo_garantia"),
    ("unidade de tempo de garantia", "unidade_garantia"),
    ("marca", "marca"),
    ("modelo", "modelo"),
    ("quantidade de caracteres", "qtd_chars"),
]


def _norm(text: str) -> str:
    text = str(text or "").strip().lower()
    text = text.replace("\n", " ")
    return re.sub(r"\s+", " ", text)


def _map_headers(header_row: tuple[Any, ...]) -> dict[str, int]:
    """Mapa chave lógica → índice de coluna (0-based)."""
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        if cell is None:
            continue
        n = _norm(cell)
        for prefix, key in HEADER_ALIASES:
            if n.startswith(prefix) or n == prefix.rstrip(":"):
                mapping.setdefault(key, idx)
                break
        # match exato curto
        if n == "fotos":
            mapping.setdefault("fotos", idx)
        if n == "sku":
            mapping.setdefault("sku", idx)
        if n == "marca":
            mapping.setdefault("marca", idx)
        if n == "modelo":
            mapping.setdefault("modelo", idx)
    return mapping


def _find_header_row(ws) -> int:
    """Retorna índice 1-based da linha de headers (geralmente 3)."""
    for r in range(1, 8):
        vals = [ws.cell(r, c).value for c in range(1, min(ws.max_column, 40) + 1)]
        joined = " ".join(_norm(v) for v in vals if v)
        if "sku" in joined and ("preço" in joined or "preco" in joined):
            return r
    return 3


def _find_data_start(ws, header_row: int, colmap: dict[str, int] | None = None) -> int:
    """
    Primeira linha de dados após ajuda.

    Pula linhas com MergedCell nas colunas críticas (SKU/Título) — no template
    ML a row 7 costuma ser faixa mesclada de UI.
    """
    start = header_row + 4  # após Obrigatório + ~3 linhas de ajuda
    probe_cols = []
    if colmap:
        for key in ("sku", "titulo", "preco", "marca"):
            if key in colmap:
                probe_cols.append(colmap[key] + 1)
    if not probe_cols:
        probe_cols = [1, 6, 8]

    for r in range(start, header_row + 12):
        # Se alguma coluna crítica é MergedCell, pula a linha
        if any(isinstance(ws.cell(r, c), MergedCell) for c in probe_cols):
            continue
        v = ws.cell(r, 1).value
        text = _norm(v)
        if text in {"obrigatório", "obrigatorio"}:
            continue
        if text and any(
            x in text
            for x in (
                "buscar",
                "máximo",
                "maximo",
                "este dado",
                "obrigat",
                "informe o produto",
                "digite apenas",
            )
        ):
            continue
        return r
    return start + 1


def _as_int_cm(raw: Any) -> Any:
    if raw in (None, "", "nan"):
        return ""
    try:
        return int(round(float(str(raw).replace(",", "."))))
    except (TypeError, ValueError):
        return ""


def _as_float(raw: Any, ndigits: int = 2) -> Any:
    if raw in (None, "", "nan"):
        return ""
    try:
        return round(float(str(raw).replace(",", ".")), ndigits)
    except (TypeError, ValueError):
        return ""


def _fotos_cell(raw: Any) -> str:
    if raw in (None, "", "nan"):
        return ""
    parts = [
        p.strip()
        for p in re.split(r"[\n,;]+", str(raw))
        if p.strip().startswith("http")
    ]
    return ",".join(parts)


def _titulo(nome: str) -> str:
    t = re.sub(r"\s+", " ", str(nome or "")).strip()
    if len(t) <= 60:
        return t
    return t[:60].rsplit(" ", 1)[0].strip()


def _produto_para_valores(row: dict[str, Any]) -> dict[str, Any]:
    titulo = _titulo(row.get("Nome", ""))
    sku = str(row.get("SKU", "")).strip()
    return {
        "titulo": titulo,
        "qtd_chars": len(titulo),
        "condicao": "Novo",
        "gtin": "O produto não tem código cadastrado",
        "fotos": _fotos_cell(row.get("Fotos", "")),
        "sku": sku,
        "estoque": _as_int_cm(row.get("Estoque", 2)) or 2,
        "preco": _as_float(row.get("Preço", "")),
        "descricao": str(row.get("Descrição", "") or ""),
        "largura": _as_int_cm(row.get("Largura (cm)", "")),
        "altura": _as_int_cm(row.get("Altura (cm)", "")),
        "profundidade": _as_int_cm(row.get("Comprimento (cm)", "")),
        "peso": _as_float(row.get("Peso (kg)", ""), 3),
        "tipo_anuncio": "Clássico",
        "forma_envio": "Mercado Envios",
        "custo_envio": "",
        "retirar": "Não",
        "tipo_garantia": "Garantia do vendedor",
        "tempo_garantia": 12,
        "unidade_garantia": "meses",
        "marca": "Wago",
        "modelo": sku,
    }


def fill_ml(
    master_csv: Path | str,
    *,
    sheet: str = "Outros",
    output: Path | str | None = None,
    template: Path | str | None = None,
) -> Path:
    """
    Copia Anunciar-ML.xlsx e preenche a aba escolhida com produtos do master.
    """
    ensure_dirs()
    tpl = Path(template) if template else TEMPLATE
    if not tpl.exists():
        raise SystemExit(f"Template ML ausente: {tpl}")

    master_path = Path(master_csv)
    if not master_path.exists():
        raise SystemExit(f"Master não encontrado: {master_path}")

    df = pd.read_csv(master_path, sep=";", dtype=str, encoding="utf-8-sig").fillna(
        ""
    )
    if df.empty:
        raise SystemExit("Master sem produtos.")

    if output:
        out_path = Path(output)
    else:
        stamp = datetime.now().strftime("%d-%m_%H%M")
        out_path = OUT / f"Anunciar-ML-preenchido_{stamp}.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Cópia binária do template para preservar validações
    out_path.write_bytes(tpl.read_bytes())

    wb = load_workbook(out_path)
    if sheet not in wb.sheetnames:
        wb.close()
        raise SystemExit(
            f"Aba {sheet!r} não existe. Disponíveis: {wb.sheetnames}"
        )

    ws = wb[sheet]
    header_row = _find_header_row(ws)
    headers = tuple(
        ws.cell(header_row, c).value for c in range(1, ws.max_column + 1)
    )
    colmap = _map_headers(headers)
    if "titulo" not in colmap and "sku" not in colmap:
        wb.close()
        raise SystemExit(
            f"Não foi possível mapear colunas na aba {sheet!r} (header row {header_row})."
        )

    data_start = _find_data_start(ws, header_row, colmap)

    # Limpa linhas de dados antigas (mantém template até data_start-1)
    max_clear = min(ws.max_row, data_start + max(len(df) + 50, 200))
    for r in range(data_start, max_clear + 1):
        for c in range(1, ws.max_column + 1):
            _set_cell(ws, r, c, None)

    for i, row in enumerate(df.to_dict(orient="records")):
        valores = _produto_para_valores(row)
        excel_row = data_start + i
        for key, col_idx in colmap.items():
            if key not in valores:
                continue
            _set_cell(ws, excel_row, col_idx + 1, valores[key])

    wb.save(out_path)
    wb.close()

    latest_sheet = OUT / f"Anunciar-ML-preenchido-{sheet}.xlsx"
    latest_sheet.write_bytes(out_path.read_bytes())
    # Atalho genérico aponta para Outros (aba default do fluxo)
    if sheet == "Outros":
        (OUT / "Anunciar-ML-preenchido.xlsx").write_bytes(out_path.read_bytes())

    print(
        f"ML preenchido: {out_path}\n"
        f"  Aba: {sheet} | Header row: {header_row} | "
        f"Dados a partir da row {data_start} | Produtos: {len(df)}"
    )
    print(f"Cópia latest: {latest_sheet}")
    print(f"Colunas mapeadas: {sorted(colmap)}")
    return out_path
