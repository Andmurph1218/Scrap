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
    assert row["Nome"] == "Sensor Capacitivo CA18CBF08BPA2IO"
    assert row["Marca"] == "Carlo Gavazzi"
    assert row["Descrição"].startswith("Sensor de proximidade capacitivo")
    assert "Distância nominal de operação: 8 mm" in row["Descrição"]
    assert "123" in row["Preço"].replace(",", ".")
    assert row["MPN (Cód. Exclusivo Modelo Fabricante)"] == "CA18CBF08BPA2IO"


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
    assert float(preco.replace(",", ".")) == pytest.approx(123.45)
