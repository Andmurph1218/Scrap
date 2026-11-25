"""Exportador Gavazzi → CSV de importação Nuvemshop.

Reusa o template oficial e os helpers de `wago.fill_nuvemshop`. Como o CSV
da Nuvemshop não tem coluna de imagens, também grava `gavazzi-imagens.json`
({codigo: [urls]}) para uso posterior pelo módulo `photos`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from wago.config import OUT, ROOT
from wago.fill_nuvemshop import _cabecalho_template, _linha_nuvemshop, _publicar_csv
from wago.images import _slug

TEMPLATE = ROOT / "templates" / "produtos-Nuvemshop.csv"
MARCA = "Carlo Gavazzi"
MARGEM = 1.55
_LARGURA_MAX = 80


def _base_descritiva(titulo: str, codigo: str) -> str:
    """Remove marca e código do título, devolvendo só a parte descritiva."""
    base = str(titulo or "")
    if codigo:
        base = re.sub(rf"\b{re.escape(codigo)}\b", " ", base, flags=re.IGNORECASE)
    base = re.sub(r"\bcarlo\s+gavazzi\b", " ", base, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", base).strip(" -–—,")


def _nome_limpo(titulo: str, codigo: str, *, limite: int | None = None) -> str:
    """Padroniza '[Nome descritivo] Carlo Gavazzi [CODIGO]' (SEO do ML).

    Marca e código aparecem uma única vez, no final. Com `limite` (ML: 60
    chars), trunca a parte descritiva com reticências e preserva o sufixo.
    """
    base = _base_descritiva(titulo, codigo)
    sufixo = f"{MARCA} {codigo}".strip() or MARCA
    if not base:
        return sufixo
    nome = f"{base} {sufixo}"
    if limite and len(nome) > limite:
        orcamento = limite - len(sufixo) - 2  # espaço + reticência
        if orcamento <= 0:
            return sufixo
        corte = base[:orcamento]
        corte = (corte.rsplit(" ", 1)[0] or corte).rstrip(" -–—,")
        nome = f"{corte}… {sufixo}"
    return nome


def _linha(produto: dict[str, Any], header: list[str]) -> dict[str, Any]:
    codigo = str(produto.get("codigo") or "").strip()
    titulo_raw = str(produto.get("titulo_pt") or produto.get("titulo") or codigo).strip()
    nome = _nome_limpo(titulo_raw, codigo)
    base_slug = _base_descritiva(titulo_raw, codigo)
    descricao = str(produto.get("descricao_pt") or produto.get("descricao") or "")
    specs_pt = produto.get("specs_pt") or {}
    valores_en = produto.get("especificacoes") or {}
    if specs_pt:
        linhas = "\n".join(f"{nome_pt}: {valores_en.get(chave_en, '')}"
                           for chave_en, nome_pt in specs_pt.items()
                           if chave_en in valores_en)
        if linhas:
            descricao = f"{descricao}\n\nESPECIFICAÇÕES TÉCNICAS\n{linhas}".strip()
    custo = produto.get("preco")
    row = {
        "Identificador URL": _slug(f"{codigo}-{base_slug}"),
        "Nome": nome[:254],
        "SKU": codigo,
        "MPN (Cód. Exclusivo Modelo Fabricante)": codigo,
        "Marca": MARCA,
        "Descrição": descricao,
        "Preço": f"{round(float(custo) * MARGEM, 2):.2f}" if custo is not None else "",
        "Custo": f"{float(custo):.2f}" if custo is not None else "",
        "Estoque": "2",
        "Exibir na loja": "SIM",
        "Produto Físico": "SIM",
        "Tags": "Gavazzi, automação industrial",
    }
    return _linha_nuvemshop(row, header)


def _xlsx_legivel(df: pd.DataFrame, dest: Path) -> Path:
    """Versão .xlsx formatada para leitura humana: larguras ajustadas (máx 80),
    wrap text na coluna Descrição e alturas automáticas."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Produtos"
    ws.append(list(df.columns))
    for cel in ws[1]:
        cel.font = Font(bold=True)
    for _, linha in df.iterrows():
        ws.append(["" if pd.isna(v) else str(v) for v in linha])

    col_desc = list(df.columns).index("Descrição") + 1 if "Descrição" in df.columns else None
    for idx, col in enumerate(df.columns, start=1):
        maior = max(
            [len(str(col))]
            + [max((len(parte) for parte in str(v).split("\n")), default=0)
               for v in df[col].head(200)],
        )
        ws.column_dimensions[get_column_letter(idx)].width = min(maior + 2, _LARGURA_MAX)
    if col_desc:
        for row_cells in ws.iter_rows(min_row=2, min_col=col_desc, max_col=col_desc):
            row_cells[0].alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(dest)
    return dest


def exportar(produtos: list[dict[str, Any]], saida: Path | str | None = None,
             *, force: bool = True) -> dict[str, Path]:
    """Gera o CSV Nuvemshop + XLSX legível + gavazzi-imagens.json. Devolve os caminhos."""
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
    xlsx_path = _xlsx_legivel(df, csv_path.with_suffix(".xlsx"))

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

    return {"csv": csv_path, "xlsx": xlsx_path, "imagens": imagens_path}
