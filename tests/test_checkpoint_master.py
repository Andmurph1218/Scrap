"""Checkpoint do master: grava antes de registrar o SKU e recupera o lote."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wago.export import (  # noqa: E402
    carregar_checkpoint_master,
    gravar_checkpoint_master,
    validar_registro_master,
)
from wago.scrape import incorporar_produto, recuperar_checkpoint  # noqa: E402


def _isolado(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr("wago.export.STATE", tmp_path)
    skus = tmp_path / "skus_processados.txt"
    monkeypatch.setattr("wago.state.SKUS_PROCESSADOS", skus)
    return skus


def _produto(**extra):
    base = {
        "SKU": "221-413",
        "Nome": "Conector",
        "Custo": 7.18,
        "Preço": 13.13,
        "IPI": 0.7,
    }
    base.update(extra)
    return base


def test_ipi_zero_passa_e_preco_ausente_nao():
    assert validar_registro_master(_produto(IPI=0)) is None
    assert validar_registro_master(_produto(Preço="")) == "Preço ausente"
    assert validar_registro_master(_produto(Custo="abc")) == "Custo inválido"
    assert validar_registro_master(_produto(IPI=-1)) == "IPI negativo"


def test_checkpoint_deduplica_por_sku(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    gravar_checkpoint_master(
        [_produto(), _produto(Nome="Conector novo", Preço=20)],
        1,
    )
    rows = carregar_checkpoint_master(1)
    assert len(rows) == 1
    assert rows[0]["Nome"] == "Conector novo"
    assert rows[0]["SKU"] == "221-413"


def test_sku_so_e_registrado_depois_do_checkpoint(tmp_path, monkeypatch):
    skus = _isolado(tmp_path, monkeypatch)
    produtos: list[dict] = []
    vistos: set[str] = set()
    assert (
        incorporar_produto(
            produtos, _produto(), lote_numero=2, skus_validos=vistos
        )
        is None
    )
    assert "221-413" in vistos
    assert skus.read_text(encoding="utf-8").strip() == "221-413"
    rows = carregar_checkpoint_master(2)
    assert rows[0]["SKU"] == "221-413"


def test_falha_ao_gravar_nao_registra_sku(tmp_path, monkeypatch):
    skus = _isolado(tmp_path, monkeypatch)

    def falha(*_args, **_kwargs):
        raise OSError("disco")

    monkeypatch.setattr("wago.scrape.gravar_checkpoint_master", falha)
    produtos: list[dict] = []
    vistos: set[str] = set()
    with pytest.raises(OSError):
        incorporar_produto(
            produtos, _produto(), lote_numero=1, skus_validos=vistos
        )
    assert produtos == []
    assert vistos == set()
    assert not skus.exists()


def test_recupera_lote_e_registra_sku_que_faltou(tmp_path, monkeypatch):
    skus = _isolado(tmp_path, monkeypatch)
    gravar_checkpoint_master([_produto(), _produto(SKU="sem-preco", Preço="")], 4)
    vistos: set[str] = set()
    rows = recuperar_checkpoint(4, vistos)
    assert [r["SKU"] for r in rows] == ["221-413"]
    assert vistos == {"221-413"}
    assert "221-413" in skus.read_text(encoding="utf-8")
    assert "SEM-PRECO" not in skus.read_text(encoding="utf-8")


def test_checkpoint_ilegivel_nao_e_sobrescrito(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    path = tmp_path / "checkpoint_master_lote_3.csv"
    path.write_text("sku;nome\n", encoding="utf-8")

    def falha(*_args, **_kwargs):
        raise ValueError("quebrado")

    monkeypatch.setattr("wago.export.pd.read_csv", falha)
    with pytest.raises(SystemExit, match="não foi sobrescrito"):
        carregar_checkpoint_master(3)
    assert path.read_text(encoding="utf-8") == "sku;nome\n"
