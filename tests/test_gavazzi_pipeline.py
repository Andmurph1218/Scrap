"""Testes do pipeline Gavazzi (orquestração com mocks, sem rede)."""

from __future__ import annotations

import json

import pytest

from wago.gavazzi import pipeline


def _planilha(tmp_path):
    arq = tmp_path / "precos.csv"
    arq.write_text(
        "codigo;preco\nCA18CBF08BPA2IO;100,00\nRM1D060D10;50,00\nNOPE;10,00\n",
        encoding="utf-8")
    return arq


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline.discover, "LINKS_JSON", tmp_path / "links.json")
    monkeypatch.setattr(pipeline.translator, "CACHE_TRADUCOES", tmp_path / "trad.json")
    monkeypatch.setattr(pipeline, "FALHAS_JSON", tmp_path / "falhas.json")
    monkeypatch.setattr(pipeline.scraper, "CACHE_HTML", tmp_path / "html")

    def fake_resolver(codigo, mapa, **kw):
        return {
            "CA18CBF08BPA2IO": "https://x/product/CA18CBF08BPA2IO",
            "RM1D060D10": "https://x/product/RM1D060D10",
        }.get(codigo)

    monkeypatch.setattr(pipeline.discover, "resolver_url", fake_resolver)
    monkeypatch.setattr(pipeline.discover, "carregar_mapa", lambda **k: {})

    def fake_scrape(url, codigo="", **kw):
        return {
            "codigo": codigo or "X",
            "titulo": codigo,
            "descricao": "desc",
            "especificacoes": {},
            "imagens": ["https://x/img.png"],
            "url": url,
        }

    monkeypatch.setattr(pipeline.scraper, "scrapear_produto", fake_scrape)
    monkeypatch.setattr(pipeline.translator, "traduzir",
                        lambda p, **kw: {**p, "titulo_pt": "T " + p["codigo"],
                                         "descricao_pt": "D", "specs_pt": {}})


def test_pipeline_completo(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    res = pipeline.rodar(_planilha(tmp_path), saida=tmp_path)
    assert res["processados"] == 2
    assert res["falhas"] == 1  # NOPE sem URL
    assert res["master"].exists()
    assert res["nuvemshop"]["csv"].exists()
    assert res["ml"]["xlsx"].exists()
    master = json.loads(res["master"].read_text(encoding="utf-8"))
    assert {p["codigo"] for p in master} == {"CA18CBF08BPA2IO", "RM1D060D10"}


def test_dry_run_nao_grava(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    res = pipeline.rodar(_planilha(tmp_path), saida=tmp_path, dry_run=True)
    assert res["processados"] == 2
    assert res["master"] is None
    assert res["nuvemshop"] is None


def test_limit(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    res = pipeline.rodar(_planilha(tmp_path), saida=tmp_path, limit=1, dry_run=True)
    assert res["processados"] == 1


def test_retomada_pula_processados(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    chamadas = []
    original = pipeline.scraper.scrapear_produto
    monkeypatch.setattr(
        pipeline.scraper, "scrapear_produto",
        lambda url, codigo="", **kw: chamadas.append(codigo) or original(url, codigo))
    master = tmp_path / "gavazzi-master_antigo.json"
    master.write_text(json.dumps(
        [{"codigo": "CA18CBF08BPA2IO", "preco": 100.0, "titulo_pt": "T"}]),
        encoding="utf-8")
    res = pipeline.rodar(_planilha(tmp_path), saida=tmp_path, dry_run=True)
    assert res["processados"] == 2  # 1 retomado + 1 novo
    assert chamadas == ["RM1D060D10"]


def test_falha_de_scrape_registrada(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        pipeline.scraper, "scrapear_produto",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("HTTP 500")))
    res = pipeline.rodar(_planilha(tmp_path), saida=tmp_path, dry_run=True)
    assert res["processados"] == 0
    assert res["falhas"] == 3  # 2 HTTP 500 + NOPE sem URL
    falhas = json.loads(pipeline.FALHAS_JSON.read_text(encoding="utf-8"))
    assert "CA18CBF08BPA2IO" in falhas
    assert "NOPE" in falhas
