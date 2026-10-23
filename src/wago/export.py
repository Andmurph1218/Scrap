"""Exporta planilha mestra e CSVs das plataformas."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from wago.config import LATEST_NUVEMSHOP_CSV, OUT, ensure_dirs

COLUNAS_NUVEMSHOP = [
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


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, sep=";", index=False, encoding="utf-8-sig")


def salvar_csv_parcial(
    produtos: list[dict[str, Any]], nome_arq: str | Path | None = None
) -> Path | None:
    if not produtos:
        return None
    ensure_dirs()
    primeiro = produtos[0]["SKU"]
    ultimo = produtos[-1]["SKU"]
    path = Path(nome_arq) if nome_arq else OUT / (
        f"wago_nuvemshop_lote_{primeiro}_a_{ultimo}.csv"
    )
    if not path.is_absolute():
        path = OUT / path
    df = pd.DataFrame(produtos)
    _write_csv(df, path)
    _write_csv(df, LATEST_NUVEMSHOP_CSV)
    print(f"Arquivo salvo: {path}")
    return path


def exportar_planilhas(
    produtos: list[dict[str, Any]], lote_numero: int = 1
) -> dict[str, Path]:
    if not produtos:
        print("Nenhum produto coletado. Nada a exportar.")
        return {}

    ensure_dirs()
    df_master = pd.DataFrame(produtos)
    data_str = datetime.now().strftime("%d-%m")
    nome_base = f"wago lote {lote_numero} {data_str}"

    paths: dict[str, Path] = {}

    master_path = OUT / f"{nome_base} master.csv"
    _write_csv(df_master, master_path)
    paths["master"] = master_path

    # Inclui Fotos no latest nuvemshop (útil para vincular_fotos)
    nuvem_cols = [c for c in COLUNAS_NUVEMSHOP if c in df_master.columns]
    df_nuvem = df_master[nuvem_cols]
    # CSV de import Nuvemshop (sem Fotos — a API cuida depois)
    nuvem_path = OUT / f"{nome_base} nuvemshop.csv"
    _write_csv(df_nuvem, nuvem_path)
    paths["nuvemshop"] = nuvem_path

    # Latest com Fotos para a etapa photos
    latest_cols = list(dict.fromkeys(nuvem_cols + ["Fotos", "SKU", "IPI", "NCM"]))
    latest_cols = [c for c in latest_cols if c in df_master.columns]
    _write_csv(df_master[latest_cols], LATEST_NUVEMSHOP_CSV)
    paths["latest"] = LATEST_NUVEMSHOP_CSV

    df_bling = pd.DataFrame(
        {
            "Código": df_master["SKU"],
            "Descrição": df_master["Nome"],
            "Unidade": "UN",
            "NCM": df_master["NCM"],
            "Origem": "0",
            "Preço": df_master["Preço"],
            "Valor IPI fixo": df_master["IPI"],
            "Situação": "Ativo",
            "Estoque": df_master["Estoque"],
            "Preço de custo": df_master["Custo"],
            "Preço de compra": df_master["Custo"],
            "Peso líquido (Kg)": df_master["Peso (kg)"],
            "Peso bruto (Kg)": df_master["Peso (kg)"],
            "Largura do Produto": df_master["Largura (cm)"],
            "Altura do Produto": df_master["Altura (cm)"],
            "Profundidade do produto": df_master["Comprimento (cm)"],
            "Descrição Complementar": df_master["Descrição"],
            "Marca": "Wago",
            "URL Imagens Externas": df_master["Fotos"],
            "Condição do produto": "NOVO",
            "Frete Grátis": "Não",
            "Categoria do produto": df_master["Categorias"],
        }
    )
    bling_path = OUT / f"{nome_base} bling.csv"
    _write_csv(df_bling, bling_path)
    paths["bling"] = bling_path

    titulo_ml = (
        "Título: informe o produto, marca, modelo e destaque as"
        " características principais \nCaso crie variações, você deve criar um"
        " título geral para todas"
    )
    peso_ml = "Peso físico (kg) \nEmbalagem com o produto dentro."
    df_ml = pd.DataFrame(
        {
            titulo_ml: df_master["Nome"],
            "Condição": "Novo",
            "Fotos": df_master["Fotos"],
            "SKU": df_master["SKU"],
            "Estoque": df_master["Estoque"],
            "Preço [R$]": df_master["Preço"],
            "Descrição": df_master["Descrição"],
            "Largura (cm)": df_master["Largura (cm)"],
            "Altura (cm)": df_master["Altura (cm)"],
            "Profundidade (cm)": df_master["Comprimento (cm)"],
            peso_ml: df_master["Peso (kg)"],
            "Tipo de anúncio": "Clássico",
            "Forma de envio": "Mercado Envios",
            "Tipo de garantia": "Garantia do vendedor",
            "Tempo de garantia": "12",
            "Unidade de Tempo de garantia": "meses",
            "Marca": "Wago",
            "Modelo": df_master["SKU"],
        }
    )
    ml_path = OUT / f"{nome_base} mercadolivre.csv"
    _write_csv(df_ml, ml_path)
    paths["mercadolivre"] = ml_path

    print("Planilha Mestra e CSVs das plataformas exportados:")
    for key, path in paths.items():
        print(f"  • [{key}] {path}")
    return paths


def exportar_de_master(master_csv: Path | str, lote_numero: int = 1) -> dict[str, Path]:
    path = Path(master_csv)
    if not path.exists():
        raise SystemExit(f"Arquivo não encontrado: {path}")
    df = pd.read_csv(path, sep=";")
    return exportar_planilhas(df.to_dict(orient="records"), lote_numero)
