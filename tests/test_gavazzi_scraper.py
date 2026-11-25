"""Testes do scraper Gavazzi contra fixture HTML real (sem rede)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wago.gavazzi.scraper import ProdutoNaoEncontrado, extrair_produto

FIXTURE = Path(__file__).parent / "fixtures" / "gavazzi_produto.html"
BASE = "https://www.gavazziautomation.com"


@pytest.fixture
def produto():
    return extrair_produto(
        FIXTURE.read_text(encoding="utf-8"),
        url=f"{BASE}/en-br/product/CA18CBF08BPA2IO",
    )


def test_codigo(produto):
    assert produto["codigo"] == "CA18CBF08BPA2IO"


def test_descricao(produto):
    assert "Capacitive Proximity Sensor" in produto["descricao"]
    assert "IP67" in produto["descricao"]


def test_especificacoes(produto):
    specs = produto["especificacoes"]
    assert specs["Rated operating distance"] == "8 mm"
    assert specs["Housing type"] == "Cylindrical, threaded barrel"
    assert specs["E-Number (NO)"] == "4316091"
    assert len(specs) >= 10


def test_imagens_urls_absolutas(produto):
    imgs = produto["imagens"]
    assert imgs
    assert all(u.startswith("https://") for u in imgs)
    assert any("PIM/WEB" in u for u in imgs)


def test_url_preservada(produto):
    assert produto["url"] == f"{BASE}/en-br/product/CA18CBF08BPA2IO"


GENERICA = (
    "<html><head><title>Carlo Gavazzi Automation Components</title>"
    "<meta property='og:title' content='Carlo Gavazzi Automation Components'/>"
    "</head><body><h1>Carlo Gavazzi Automation Components</h1></body></html>"
)


def test_pagina_generica_aborta_mesmo_com_codigo_na_url():
    # rota /product/AMPF-MB2 existe na URL, mas a página é institucional
    with pytest.raises(ProdutoNaoEncontrado, match="Código não encontrado"):
        extrair_produto(GENERICA, url=f"{BASE}/en-br/product/AMPF-MB2",
                        codigo_esperado="AMPF-MB2")


def test_codigo_ausente_da_pagina_aborta():
    html = ("<html><head><title>Outro produto</title>"
            "<meta property='og:title' content='XYZ123'/></head>"
            "<body><h1>XYZ123</h1></body></html>")
    with pytest.raises(ProdutoNaoEncontrado):
        extrair_produto(html, url=f"{BASE}/en-br/product/XYZ123",
                        codigo_esperado="AMPF-MB2")


def test_codigo_presente_em_qualquer_fonte_valida():
    html = ("<html><head><meta property='og:title' content='AMPF-MB2'/></head>"
            "<body><h1>Mounting accessory</h1></body></html>")
    p = extrair_produto(html, url=f"{BASE}/en-br/product/AMPF-MB2",
                        codigo_esperado="ampf-mb2")
    assert p["codigo"] == "AMPF-MB2"  # código da planilha é autoritativo


def test_produto_minimo_sem_specs():
    html = "<html><head><meta property='og:title' content='X1'/></head><body></body></html>"
    p = extrair_produto(html, url=f"{BASE}/en-br/product/X1")
    assert p["codigo"] == "X1"
    assert p["especificacoes"] == {}
    assert p["imagens"] == []
