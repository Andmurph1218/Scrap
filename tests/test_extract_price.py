"""Testes unitários do P0: extração de preço sem max() de todos os R$."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wago.extract import _parse_brl, extrair_preco_custo  # noqa: E402


def _page_with_html(html: str) -> MagicMock:
    page = MagicMock()
    page.content.return_value = html
    return page


def test_parse_brl_formats():
    assert _parse_brl("1.234,56") == 1234.56
    assert _parse_brl("99,90") == 99.90
    assert _parse_brl("12.50") == 12.50
    assert _parse_brl("0") is None
    assert _parse_brl("abc") is None


def test_jsonld_product_offer_wins():
    html = """
    <html><body>
      <script type="application/ld+json">
      {
        "@type": "Product",
        "name": "Bornes",
        "offers": {"@type": "Offer", "price": "42.50", "priceCurrency": "BRL"}
      }
      </script>
      <div>Frete R$ 199,90</div>
      <div>Total R$ 5.000,00</div>
      <div class="price">R$ 42,50</div>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    page = _page_with_html(html)
    price = extrair_preco_custo(page, soup, soup.get_text(" ", strip=True), "221-413")
    assert price == 42.50


def test_does_not_use_max_of_all_reais():
    """Regressão P0: não deve pegar o maior R$ da página (frete/total)."""
    html = """
    <html><body>
      <script type="application/ld+json">
      {"@type":"Product","offers":{"price":"15.00","priceCurrency":"BRL"}}
      </script>
      <p>Frete R$ 89,90</p>
      <p>Pedido mínimo R$ 500,00</p>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    page = _page_with_html(html)
    price = extrair_preco_custo(page, soup, soup.get_text(" ", strip=True), "SKU1")
    assert price == 15.00
    assert price != 500.00


def test_data_price_fallback():
    html = """
    <html><body>
      <span data-price="33,20">preço</span>
      <p>Outro valor R$ 999,00</p>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    page = _page_with_html(html)
    price = extrair_preco_custo(page, soup, soup.get_text(" ", strip=True), "X")
    assert price == 33.20


def test_sku_from_url():
    from wago.discover import sku_from_url

    assert sku_from_url("https://www.wago.com/br/p/221-413") == "221-413"
    assert sku_from_url("https://www.wago.com/br/p/750-333?foo=1") == "750-333"
