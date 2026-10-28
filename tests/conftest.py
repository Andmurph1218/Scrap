from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True)
def _catalogo_isolado(tmp_path_factory, monkeypatch):
    """Nenhum teste lê ou grava o catálogo e os espelhos reais de data/state."""
    base = tmp_path_factory.mktemp("estado")
    monkeypatch.setattr("wago.config.CATALOGO", base / "catalogo.sqlite")
    monkeypatch.setattr("wago.config.OUT", base / "out")
    monkeypatch.setattr("wago.state.SKUS_PROCESSADOS", base / "skus_processados.txt")
    monkeypatch.setattr("wago.state.SKUS_SEM_PRECO", base / "skus_sem_preco.txt")
    monkeypatch.setattr("wago.state.LINKS_FILE", base / "links.json")
    return base
