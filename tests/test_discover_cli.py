"""discover só baixa o sitemap de novo quando --refresh é passado."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wago.__main__ import main  # noqa: E402


def test_discover_reusa_cache(monkeypatch):
    visto: dict[str, bool] = {}

    def fake(force_refresh: bool = False):
        visto["force"] = force_refresh
        return []

    monkeypatch.setattr("wago.discover.carregar_links", fake)
    main(["discover"])
    assert visto["force"] is False


def test_discover_refresh_forca_download(monkeypatch):
    visto: dict[str, bool] = {}

    def fake(force_refresh: bool = False):
        visto["force"] = force_refresh
        return []

    monkeypatch.setattr("wago.discover.carregar_links", fake)
    main(["discover", "--refresh"])
    assert visto["force"] is True
