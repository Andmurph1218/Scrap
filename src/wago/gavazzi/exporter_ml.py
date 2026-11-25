"""Exportador Gavazzi → Anunciar-ML.xlsx (template oficial do Mercado Livre).

Reusa a máquina de escrita raw-XML de `wago.fill_ml`, que preserva
dataValidations, fórmulas e sharedStrings do template.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from wago.config import OUT, ROOT
from wago.fill_ml import (
    _SharedStrings,
    _aplicar_celulas,
    _caminho_da_aba,
    _col_letter,
    _find_data_start,
    _find_header_row,
    _formulas,
    _linhas_para_produtos,
    _map_headers,
    _norm,
    _produto_para_valores,
    _substituir_partes,
    _titulo,
    inferir_aba_ml,
)

TEMPLATE = ROOT / "templates" / "Anunciar-ML.xlsx"
MARCA = "Carlo Gavazzi"
_ABAS_IGNORAR = {"Ajuda", "extra info", "Textos jurídicos"}


def _valores(produto: dict[str, Any]) -> dict[str, Any]:
    """Converte o produto traduzido para as chaves lógicas do fill_ml."""
    codigo = str(produto.get("codigo") or "").strip()
    titulo = _titulo(str(produto.get("titulo_pt") or produto.get("titulo") or codigo))
    descricao = str(produto.get("descricao_pt") or produto.get("descricao") or "")
    preco = produto.get("preco")
    row_like = {
        "Nome": titulo,
        "SKU": codigo,
        "Preço": preco if preco is not None else "",
        "Descrição": descricao,
        "Fotos": ",".join(produto.get("imagens") or []),
        "Estoque": "2",
        "Categorias": "",
    }
    valores = _produto_para_valores(row_like, sheet="Outros")
    valores["marca"] = MARCA
    valores["fabricante"] = MARCA
    valores["modelo"] = codigo or valores["modelo"]
    valores["modelo_alfanumerico"] = codigo or valores["modelo_alfanumerico"]
    return valores


def _colunas_specs(ws, header_row: int, mapeadas: set[int]) -> list[tuple[int, str]]:
    """Colunas do template que são atributos livres (candidatas a specs)."""
    cols = []
    for c in range(1, min(ws.max_column, 56) + 1):
        if c - 1 in mapeadas:
            continue
        texto = _norm(ws.cell(header_row, c).value)
        if texto:
            cols.append((c - 1, texto))
    return cols


def _mapear_specs(produto: dict[str, Any], headers: tuple, colmap: dict[str, int],
                  colunas_livres: list[tuple[int, str]]) -> tuple[dict[str, Any], list[str]]:
    """Tenta casar cada spec a uma coluna de atributo; devolve {col_idx: valor} e sobras."""
    especs = produto.get("specs_pt") or produto.get("especificacoes") or {}
    valores_orig = produto.get("especificacoes") or {}
    updates: dict[str, Any] = {}
    sobras: list[str] = []
    for chave_en, chave_pt in especs.items():
        valor = valores_orig.get(chave_en, "")
        alvo = _norm(str(chave_pt or chave_en))
        achou = None
        for col_idx, header_norm in colunas_livres:
            if alvo and (header_norm.startswith(alvo) or alvo in header_norm):
                achou = col_idx
                break
        if achou is not None and valor:
            updates[f"__col_{achou}"] = valor
        elif valor:
            sobras.append(f"{chave_pt or chave_en}: {valor}")
    return updates, sobras


def exportar(produtos: list[dict[str, Any]], saida: Path | str | None = None,
             *, template: Path | str | None = None) -> dict[str, Any]:
    """Preenche o Anunciar-ML.xlsx. Devolve {xlsx, resumo(Path)}."""
    saida = Path(saida) if saida else OUT
    saida.mkdir(parents=True, exist_ok=True)
    tpl = Path(template) if template else TEMPLATE
    if not tpl.exists():
        raise SystemExit(f"Template ML ausente: {tpl}")

    stamp = datetime.now().strftime("%d-%m_%H%M%S")
    out_path = saida / f"gavazzi-Anunciar-ML_{stamp}.xlsx"
    origem = tpl

    avisos: list[str] = []
    rejeitados: list[dict[str, str]] = []
    planos: dict[str, dict[str, Any]] = {}

    wb = load_workbook(origem)
    try:
        abas_validas = [n for n in wb.sheetnames if n not in _ABAS_IGNORAR]
        por_aba: dict[str, list[dict[str, Any]]] = {}
        for p in produtos:
            row_like = {"Categorias": "", "Nome": str(p.get("titulo_pt") or p.get("titulo") or ""),
                        "Descrição": str(p.get("descricao_pt") or p.get("descricao") or "")}
            aba = inferir_aba_ml(row_like, abas_validas)
            if aba == "Outros" and "outros" not in _norm(row_like["Nome"]):
                avisos.append(
                    f"{p.get('codigo')}: categoria não identificada → aba 'Outros'")
            por_aba.setdefault(aba, []).append(p)

        for aba, itens in por_aba.items():
            ws = wb[aba]
            header_row = _find_header_row(ws)
            headers = tuple(
                ws.cell(header_row, c).value
                for c in range(1, min(ws.max_column, 56) + 1))
            colmap = _map_headers(headers)
            if "titulo" not in colmap or "sku" not in colmap:
                raise SystemExit(f"Não foi possível mapear colunas na aba {aba!r}.")
            data_start = _find_data_start(ws, header_row, colmap)
            livres, _ = _linhas_para_produtos(
                ws, data_start, colmap["sku"] + 1, len(itens))
            if len(livres) < len(itens):
                raise SystemExit(f"Aba {aba!r} sem linhas livres suficientes.")
            colunas_livres = _colunas_specs(ws, header_row, set(colmap.values()))

            updates: dict[str, Any] = {}
            forcar_refs: set[str] = set()
            for excel_row, p in zip(livres[: len(itens)], itens):
                valores = _valores(p)
                formulas = _formulas(ws, excel_row, colmap)
                spec_cols, sobras = _mapear_specs(p, headers, colmap, colunas_livres)
                if sobras:
                    valores["descricao"] = (
                        valores.get("descricao", "")
                        + "\n\nESPECIFICAÇÕES TÉCNICAS\n" + "\n".join(sobras))
                    avisos.append(
                        f"{p.get('codigo')}: {len(sobras)} spec(s) sem coluna de atributo "
                        f"na aba {aba!r} → movidas para a descrição")
                for key, col_idx in colmap.items():
                    valor = valores.get(key)
                    if valor in ("", None) or key in {"qtd_chars", "custo_envio", "retirar"}:
                        continue
                    ref = f"{_col_letter(col_idx + 1)}{excel_row}"
                    if ref in formulas:
                        continue
                    if key == "fotos":
                        forcar_refs.add(ref)
                    updates[ref] = valor
                for marcador, valor in spec_cols.items():
                    col_idx = int(marcador.removeprefix("__col_"))
                    ref = f"{_col_letter(col_idx + 1)}{excel_row}"
                    if ref not in formulas:
                        updates[ref] = valor
            planos[aba] = {
                "parte": _caminho_da_aba(origem, aba),
                "updates": updates,
                "forcar_refs": forcar_refs,
            }
    finally:
        wb.close()

    with zipfile.ZipFile(origem) as arquivo:
        ss_xml = (arquivo.read("xl/sharedStrings.xml")
                  if "xl/sharedStrings.xml" in arquivo.namelist() else None)
        shared_strings = _SharedStrings(ss_xml) if ss_xml is not None else None
        partes: dict[str, bytes] = {}
        for info in planos.values():
            xml_aba = arquivo.read(info["parte"])
            partes[info["parte"]] = _aplicar_celulas(
                xml_aba, info["updates"],
                shared_strings=shared_strings,
                forcar_Refs=info["forcar_refs"])
    if shared_strings is not None:
        partes["xl/sharedStrings.xml"] = shared_strings.to_bytes()
    _substituir_partes(origem, out_path, partes)

    resumo = {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "exportados": len(produtos),
        "abas_preenchidas": {a: len(p["updates"]) for a, p in planos.items()},
        "avisos": avisos,
        "rejeitados": rejeitados,
        "template": str(tpl),
    }
    resumo_path = out_path.with_suffix(".resumo.json")
    resumo_path.write_text(
        json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"xlsx": out_path, "resumo": resumo_path}
