"""Etapa 4a: preenche o CSV oficial de importação Nuvemshop."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from wago.config import OUT, ROOT, ensure_dirs

TEMPLATE = ROOT / "templates" / "produtos-Nuvemshop.csv"

# Ordem oficial do template anexado
COLUNAS = [
    "Identificador URL",
    "Nome",
    "Categorias",
    "Nome da variação 1",
    "Valor da variação 1",
    "Nome da variação 2",
    "Valor da variação 2",
    "Nome da variação 3",
    "Valor da variação 3",
    "Preço",
    "Preço promocional",
    "Peso (kg)",
    "Altura (cm)",
    "Largura (cm)",
    "Comprimento (cm)",
    "Estoque",
    "SKU",
    "Código de barras",
    "Exibir na loja",
    "Frete gratis",
    "Descrição",
    "Tags",
    "Título para SEO",
    "Descrição para SEO",
    "Marca",
    "Produto Físico",
    "MPN (Cód. Exclusivo Modelo Fabricante)",
    "Sexo",
    "Faixa etária",
    "Custo",
    "Visibilidade",
]


def _carregar_master(master_csv: Path | str) -> pd.DataFrame:
    path = Path(master_csv)
    if not path.exists():
        raise SystemExit(f"Master não encontrado: {path}")
    return pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")


def _linha_nuvemshop(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {c: "" for c in COLUNAS}
    for col in COLUNAS:
        if col in row and str(row[col]).strip() not in ("", "nan"):
            out[col] = row[col]

    # Defaults seguros do template
    out.setdefault("Exibir na loja", "SIM")
    if not out.get("Exibir na loja"):
        out["Exibir na loja"] = "SIM"
    if not out.get("Frete gratis"):
        out["Frete gratis"] = "NÃO"
    if not out.get("Produto Físico"):
        out["Produto Físico"] = "SIM"
    if not out.get("Marca"):
        out["Marca"] = "Wago"
    if not out.get("Visibilidade"):
        out["Visibilidade"] = "Visível"
    if not out.get("Estoque"):
        out["Estoque"] = "2"
    if not out.get("MPN (Cód. Exclusivo Modelo Fabricante)"):
        out["MPN (Cód. Exclusivo Modelo Fabricante)"] = out.get("SKU", "")
    return out


def fill_nuvemshop(
    master_csv: Path | str,
    *,
    output: Path | str | None = None,
    template: Path | str | None = None,
) -> Path:
    """
    Gera CSV de importação Nuvemshop preenchido a partir do master scrape.

    Usa as colunas do template oficial (descarta linhas de exemplo).
    """
    ensure_dirs()
    tpl = Path(template) if template else TEMPLATE
    if not tpl.exists():
        raise SystemExit(
            f"Template Nuvemshop ausente: {tpl}\n"
            "Coloque o arquivo em templates/produtos-Nuvemshop.csv"
        )

    # Valida header do template
    header = pd.read_csv(tpl, sep=";", nrows=0, encoding="utf-8-sig").columns.tolist()
    missing = [c for c in COLUNAS if c not in header]
    if missing:
        print(f"Aviso: template sem colunas {missing}; usando ordem interna.")

    df_master = _carregar_master(master_csv)
    if df_master.empty:
        raise SystemExit("Master sem produtos.")

    linhas = [_linha_nuvemshop(r) for r in df_master.to_dict(orient="records")]
    df_out = pd.DataFrame(linhas, columns=COLUNAS)

    if output:
        out_path = Path(output)
    else:
        stamp = datetime.now().strftime("%d-%m_%H%M")
        out_path = OUT / f"produtos-Nuvemshop-preenchido_{stamp}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_csv(out_path, sep=";", index=False, encoding="utf-8-sig")

    latest = OUT / "produtos-Nuvemshop-preenchido.csv"
    df_out.to_csv(latest, sep=";", index=False, encoding="utf-8-sig")

    print(f"Nuvemshop preenchido: {out_path} ({len(df_out)} produtos)")
    print(f"Cópia latest: {latest}")
    return out_path
