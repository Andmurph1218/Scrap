"""Testes do discover Gavazzi: URL direta primeiro, sitemap como fallback."""

from __future__ import annotations

import json

from wago.gavazzi import discover

SITEMAP_INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.gavazziautomation.com/en-br/sitemap-type/products/sitemap.xml</loc></sitemap>
</sitemapindex>"""

SITEMAP_PRODUCTS = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.gavazziautomation.com/en-br/product/CA18CBF08BPA2IO</loc></url>
  <url><loc>https://www.gavazziautomation.com/en-br/product/RM1D060D10?x=1</loc></url>
  <url><loc>https://www.gavazziautomation.com/en-br/company/who-we-are</loc></url>
</urlset>"""


class FakeResp:
    def __init__(self, status, text="", content=b""):
        self.status_code = status
        self.text = text
        self.content = content or text.encode()


def test_extrai_codigos_do_sitemap():
    mapa = discover._extrair_produtos_sitemap(SITEMAP_PRODUCTS)
    assert mapa == {
        "CA18CBF08BPA2IO": "https://www.gavazziautomation.com/en-br/product/CA18CBF08BPA2IO",
        "RM1D060D10": "https://www.gavazziautomation.com/en-br/product/RM1D060D10",
    }


def test_url_direta_quando_pagina_existe(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return FakeResp(200, "<html>ok</html>")

    monkeypatch.setattr(discover, "_get", fake_get)
    url = discover.resolver_url("CA18CBF08BPA2IO", mapa={})
    assert url == "https://www.gavazziautomation.com/en-br/product/CA18CBF08BPA2IO"
    assert calls == [url]


def test_fallback_sitemap_quando_404(monkeypatch):
    def fake_get(url, **kw):
        return FakeResp(404)

    monkeypatch.setattr(discover, "_get", fake_get)
    mapa = {"XYZ": "https://www.gavazziautomation.com/en-br/product/XYZ"}
    assert discover.resolver_url("XYZ", mapa=mapa) == mapa["XYZ"]


def test_resolver_sem_url(monkeypatch):
    monkeypatch.setattr(discover, "_get", lambda u, **k: FakeResp(404))
    assert discover.resolver_url("NOPE", mapa={}) is None


def test_baixar_mapa_percorre_sitemaps(monkeypatch, tmp_path):
    respostas = {
        discover.SITEMAP_URL: FakeResp(200, SITEMAP_INDEX),
        "https://www.gavazziautomation.com/en-br/sitemap-type/products/sitemap.xml": FakeResp(200, SITEMAP_PRODUCTS),
    }
    monkeypatch.setattr(discover, "_get", lambda u, **k: respostas[u])
    mapa = discover.baixar_mapa(destino=tmp_path / "links.json")
    assert mapa["CA18CBF08BPA2IO"].endswith("/product/CA18CBF08BPA2IO")
    salvo = json.loads((tmp_path / "links.json").read_text(encoding="utf-8"))
    assert salvo == mapa


def test_carregar_mapa_usa_cache(monkeypatch, tmp_path):
    arq = tmp_path / "links.json"
    arq.write_text(json.dumps({"A": "https://x/product/A"}), encoding="utf-8")
    monkeypatch.setattr(discover, "LINKS_JSON", arq)
    assert discover.carregar_mapa() == {"A": "https://x/product/A"}
