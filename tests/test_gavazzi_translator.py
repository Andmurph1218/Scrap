"""Testes do tradutor Gavazzi: Gemini (JSON estrito) → fallback deep-translator."""

from __future__ import annotations

import json

from wago.gavazzi import translator
from wago.taxonomia.ia import CotaEsgotada, ErroIA

PRODUTO = {
    "codigo": "CA18CBF08BPA2IO",
    "titulo": "CA18CBF08BPA2IO",
    "descricao": "Capacitive Proximity Sensor, Flush mountable, IP67",
    "especificacoes": {"Rated operating distance": "8 mm", "Housing type": "Cylindrical"},
}


class FakeProvider:
    nome = "gemini"

    def __init__(self, resposta=None, erro=None):
        self.resposta = resposta or {}
        self.erro = erro
        self.prompts = []

    def gerar_json(self, prompt):
        self.prompts.append(prompt)
        if self.erro:
            raise self.erro
        return self.resposta


def test_gemini_json_estrito():
    prov = FakeProvider(resposta={
        "titulo_pt": "Sensor Capacitivo",
        "descricao_pt": "Sensor de proximidade capacitivo, embutível, IP67",
        "specs_pt": {"Rated operating distance": "Distância nominal de operação",
                     "Housing type": "Tipo de invólucro"},
    })
    out = translator.traduzir(PRODUTO, provider=prov, usar_cache=False)
    assert out["titulo_pt"] == "Sensor Capacitivo"
    assert "capacitivo" in out["descricao_pt"].lower()
    assert out["specs_pt"]["Rated operating distance"] == "Distância nominal de operação"
    assert len(prov.prompts) == 1


def test_fallback_quando_sem_provider(monkeypatch):
    chamadas = []
    monkeypatch.setattr(translator, "_google_traduzir",
                        lambda t: chamadas.append(t) or f"[pt]{t}")
    out = translator.traduzir(PRODUTO, provider=None, usar_cache=False)
    assert out["descricao_pt"].startswith("[pt]")
    assert chamadas  # traduziu ao menos descrição


def test_fallback_quando_cota_esgotada(monkeypatch):
    prov = FakeProvider(erro=CotaEsgotada("gemini", "quota", diaria=True))
    monkeypatch.setattr(translator, "_google_traduzir", lambda t: f"[pt]{t}")
    out = translator.traduzir(PRODUTO, provider=prov, usar_cache=False)
    assert out["descricao_pt"].startswith("[pt]")


def test_fallback_quando_erro_ia(monkeypatch):
    prov = FakeProvider(erro=ErroIA("falha"))
    monkeypatch.setattr(translator, "_google_traduzir", lambda t: f"[pt]{t}")
    out = translator.traduzir(PRODUTO, provider=prov, usar_cache=False)
    assert out["descricao_pt"].startswith("[pt]")


def test_cache_evita_retraducao(tmp_path, monkeypatch):
    monkeypatch.setattr(translator, "CACHE_TRADUCOES", tmp_path / "cache.json")
    prov = FakeProvider(resposta={
        "titulo_pt": "T", "descricao_pt": "D", "specs_pt": {"a": "b"}})
    translator.traduzir(PRODUTO, provider=prov)
    translator.traduzir(PRODUTO, provider=prov)
    assert len(prov.prompts) == 1  # segunda vez veio do cache


def test_codigo_preservado_na_traducao():
    prov = FakeProvider(resposta={
        "titulo_pt": "Sensor", "descricao_pt": "d", "specs_pt": {}})
    out = translator.traduzir(PRODUTO, provider=prov, usar_cache=False)
    assert out["codigo"] == "CA18CBF08BPA2IO"
