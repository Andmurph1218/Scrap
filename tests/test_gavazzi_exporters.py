"""Testes dos exportadores Gavazzi → Nuvemshop CSV + Anunciar-ML.xlsx."""

from __future__ import annotations

import json
import zipfile

import pandas as pd
import pytest
from openpyxl import load_workbook

from wago.gavazzi import exporter_ml, exporter_nuvemshop

PRODUTOS = [
    {
        "codigo": "CA18CBF08BPA2IO",
        "titulo_pt": "Sensor Capacitivo CA18CBF08BPA2IO",
        "descricao_pt": "Sensor de proximidade capacitivo, embutível, IP67",
        "especificacoes": {"Rated operating distance": "8 mm"},
        "specs_pt": {"Rated operating distance": "Distância nominal de operação"},
        "imagens": ["https://www.gavazziautomation.com/img1.png",
                    "https://www.gavazziautomation.com/img2.png"],
        "preco": 123.45,
        "url": "https://www.gavazziautomation.com/en-br/product/CA18CBF08BPA2IO",
    },
    {
        "codigo": "RM1D060D10",
        "titulo_pt": "Relé de estado sólido RM1D060D10",
        "descricao_pt": "SSR de 1 polo, comutação CC",
        "especificacoes": {},
        "specs_pt": {},
        "imagens": [],
        "preco": 89.90,
        "url": "",
    },
]


def test_nuvemshop_colunas_do_template(tmp_path):
    out = exporter_nuvemshop.exportar(PRODUTOS, saida=tmp_path)
    df = pd.read_csv(out["csv"], sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    esperado = pd.read_csv(
        exporter_nuvemshop.TEMPLATE, sep=";", dtype=str,
        encoding="utf-8-sig", nrows=0).columns.tolist()
    assert df.columns.tolist() == esperado


def test_nuvemshop_linha_preenchida(tmp_path):
    out = exporter_nuvemshop.exportar(PRODUTOS[:1], saida=tmp_path)
    df = pd.read_csv(out["csv"], sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    row = df.iloc[0]
    assert row["SKU"] == "CA18CBF08BPA2IO"
    # Nome limpo: sem o código (que permanece no SKU e no handle)
    assert row["Nome"] == "Sensor Capacitivo"
    assert "ca18cbf08bpa2io" in row["Identificador URL"]
    assert row["Marca"] == "Carlo Gavazzi"
    assert row["Descrição"].startswith("Sensor de proximidade capacitivo")
    assert "Distância nominal de operação: 8 mm" in row["Descrição"]
    # Custo = planilha; Preço = custo * 1.55
    assert float(row["Custo"].replace(",", ".")) == pytest.approx(123.45)
    assert float(row["Preço"].replace(",", ".")) == pytest.approx(round(123.45 * 1.55, 2))
    assert row["MPN (Cód. Exclusivo Modelo Fabricante)"] == "CA18CBF08BPA2IO"


def test_nuvemshop_xlsx_legivel(tmp_path):
    out = exporter_nuvemshop.exportar(PRODUTOS[:1], saida=tmp_path)
    assert out["xlsx"].exists() and out["xlsx"].suffix == ".xlsx"
    wb = load_workbook(out["xlsx"])
    try:
        ws = wb.active
        headers = [c.value for c in ws[1]]
        col_desc = headers.index("Descrição") + 1
        assert ws.cell(2, col_desc).alignment.wrap_text is True
        assert ws.column_dimensions["A"].width <= 82
        assert ws.cell(2, headers.index("Nome") + 1).value == "Sensor Capacitivo"
    finally:
        wb.close()


def test_nuvemshop_imagens_json(tmp_path):
    out = exporter_nuvemshop.exportar(PRODUTOS, saida=tmp_path)
    imagens = json.loads(out["imagens"].read_text(encoding="utf-8"))
    assert imagens["CA18CBF08BPA2IO"] == PRODUTOS[0]["imagens"]
    assert imagens["RM1D060D10"] == []


def test_ml_preenche_template(tmp_path):
    out = exporter_ml.exportar(PRODUTOS, saida=tmp_path)
    assert out["xlsx"].exists()
    wb = load_workbook(out["xlsx"], read_only=True)
    try:
        achou = False
        for aba in wb.sheetnames:
            ws = wb[aba]
            for row in ws.iter_rows(min_row=1, max_row=60, values_only=True):
                if "CA18CBF08BPA2IO" in (str(c) for c in row if c):
                    achou = True
        assert achou, "SKU não encontrado em nenhuma aba"
    finally:
        wb.close()


def test_ml_preserva_validacoes(tmp_path):
    out = exporter_ml.exportar(PRODUTOS[:1], saida=tmp_path)
    with zipfile.ZipFile(out["xlsx"]) as z:
        xmls = [z.read(n) for n in z.namelist() if n.startswith("xl/worksheets/")]
    assert any(b"<dataValidations" in x for x in xmls)


def test_ml_resumo_e_aba_fallback(tmp_path):
    out = exporter_ml.exportar(PRODUTOS, saida=tmp_path)
    resumo = json.loads(out["resumo"].read_text(encoding="utf-8"))
    assert resumo["exportados"] == 2
    assert resumo["abas_preenchidas"]
    assert "avisos" in resumo


def test_nuvemshop_preco_formatado_br(tmp_path):
    out = exporter_nuvemshop.exportar(PRODUTOS[:1], saida=tmp_path)
    df = pd.read_csv(out["csv"], sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    # template usa "123,45" ou "123.45" — aceitar ambos com 2 casas
    preco = df.iloc[0]["Preço"]
    assert float(preco.replace(",", ".")) == pytest.approx(round(123.45 * 1.55, 2))


def test_ml_preco_com_markup(tmp_path):
    exporter_ml.exportar(PRODUTOS[:1], saida=tmp_path)
    valores = exporter_ml._valores(PRODUTOS[0])
    assert valores["preco"] == pytest.approx(round(123.45 * 1.55, 2))
