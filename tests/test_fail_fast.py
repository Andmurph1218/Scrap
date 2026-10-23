"""P0: fail-fast de credenciais."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wago.config import require_env  # noqa: E402


def test_require_env_exits_when_missing(monkeypatch):
    monkeypatch.delenv("NUVEMSHOP_STORE_ID", raising=False)
    monkeypatch.delenv("NUVEMSHOP_ACCESS_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        require_env("NUVEMSHOP_STORE_ID", "NUVEMSHOP_ACCESS_TOKEN")


def test_require_env_returns_values(monkeypatch):
    monkeypatch.setenv("NUVEMSHOP_STORE_ID", "123")
    monkeypatch.setenv("NUVEMSHOP_ACCESS_TOKEN", "tok")
    vals = require_env("NUVEMSHOP_STORE_ID", "NUVEMSHOP_ACCESS_TOKEN")
    assert vals["NUVEMSHOP_STORE_ID"] == "123"
    assert vals["NUVEMSHOP_ACCESS_TOKEN"] == "tok"
