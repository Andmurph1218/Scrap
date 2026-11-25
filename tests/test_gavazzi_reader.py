"""Testes do leitor de planilha de preços Gavazzi."""

from __future__ import annotations

import pandas as pd
import pytest

from wago.gavazzi.reader import ler_planilha


def _csv(tmp_path, texto: str, nome="precos.csv") -> str:
    p = tmp_path / nome
    p.write_text(texto, encoding="utf-8")
    return str(p)


def test_le_csv_detecta_colunas(tmp_path):
    arq = _csv(tmp_path, "codigo;descricao;preco\nCA18CBF08BPA2IO;Sensor capacitivo;123,45\n")
    itens = ler_planilha(arq)
    assert itens == [
        {"codigo": "CA18CBF08BPA2IO", "preco": 123.45, "descricao_planilha": "Sensor capacitivo"}
    ]


def test_normaliza_codigo_e_preco(tmp_path):
    arq = _csv(tmp_path, "SKU;Valor\nca18cbf08bpa2io ;R$ 1.234,56\n")
    itens = ler_planilha(arq)
    assert itens[0]["codigo"] == "CA18CBF08BPA2IO"
    assert itens[0]["preco"] == 1234.56


def test_le_xlsx(tmp_path):
    arq = tmp_path / "precos.xlsx"
    pd.DataFrame(
        [{"Código": "RM1D060D10", "Preço": "89,90", "Descrição": "SSR"}]
    ).to_excel(arq, index=False)
    itens = ler_planilha(str(arq))
    assert itens[0]["codigo"] == "RM1D060D10"
    assert itens[0]["preco"] == 89.90
    assert itens[0]["descricao_planilha"] == "SSR"


def test_override_colunas(tmp_path):
    arq = _csv(tmp_path, "ref;valor_unit;obs\nABC123;10,00;foo\n")
    itens = ler_planilha(arq, coluna_codigo="ref", coluna_preco="valor_unit")
    assert itens[0]["codigo"] == "ABC123"
    assert itens[0]["preco"] == 10.0


def test_ignora_linhas_sem_codigo_ou_preco(tmp_path):
    arq = _csv(tmp_path, "codigo;preco\n;10,00\nXYZ;abc\nQWE;50,00\n")
    itens = ler_planilha(arq)
    assert [i["codigo"] for i in itens] == ["QWE"]


def test_erro_sem_coluna_codigo(tmp_path):
    arq = _csv(tmp_path, "foo;bar\n1;2\n")
    with pytest.raises(SystemExit, match="coluna"):
        ler_planilha(arq)


def test_arquivo_inexistente(tmp_path):
    with pytest.raises(SystemExit, match="não encontrada"):
        ler_planilha(str(tmp_path / "nada.csv"))
