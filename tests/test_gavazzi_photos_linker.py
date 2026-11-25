"""Testes do linker de fotos Gavazzi → Nuvemshop (API mockada)."""

from __future__ import annotations

import json

import pytest

from wago.dashboard.service import ErroPainel, Painel
from wago.gavazzi import photos_linker


@pytest.fixture
def mapa_json(tmp_path):
    path = tmp_path / "gavazzi-imagens.json"
    path.write_text(json.dumps({
        "CA18CBF08BPA2IO": ["https://img/1.png", "https://img/2.png"],
        "SEM_FOTO": [],
        "RM1D060D10": ["https://img/3.png"],
    }), encoding="utf-8")
    return path


@pytest.fixture
def nuvemshop_mock(monkeypatch):
    """Simula loja: CA18 existe sem imagens; COMFOTO já tem imagem; SUMIDO não existe."""
    monkeypatch.setenv("NUVEMSHOP_STORE_ID", "999")
    monkeypatch.setenv("NUVEMSHOP_ACCESS_TOKEN", "tok")
    estado = {"uploads": [], "registrados": [], "limpados": []}
    monkeypatch.setattr(photos_linker, "_headers", lambda: {"h": "1"})
    monkeypatch.setattr(photos_linker, "_store_id", lambda: "999")
    monkeypatch.setattr(
        photos_linker, "carregar_mapa_skus_completo",
        lambda *a, **k: {"CA18CBF08BPA2IO": 11, "COMFOTO": 22, "CACHESKU": 33},
    )
    monkeypatch.setattr(photos_linker, "buscar_product_id_direto", lambda *a: None)
    monkeypatch.setattr(
        photos_linker, "obter_imagens_produto",
        lambda h, s, pid: [{"src": "https://cdn/foto.jpg", "id": 7}] if pid == 22 else [],
    )
    monkeypatch.setattr(
        photos_linker, "enviar_foto_robusta",
        lambda pid, url, *a, **k: estado["uploads"].append((pid, url)) or True,
    )
    monkeypatch.setattr(
        photos_linker, "limpar_imagens_produto",
        lambda h, s, pid, imgs=None: estado["limpados"].append(pid) or 1,
    )
    monkeypatch.setattr(photos_linker, "carregar_skus_foto_vinculada", lambda: set())
    monkeypatch.setattr(
        photos_linker, "registrar_foto_vinculada",
        lambda sku, pid: estado["registrados"].append(sku),
    )
    monkeypatch.setattr(photos_linker, "OUT", __import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    return estado


def test_mapa_ausente(tmp_path):
    with pytest.raises(SystemExit):
        photos_linker.carregar_mapa_imagens(tmp_path / "naoexiste.json")


def test_mapa_carrega_so_skus_com_url(mapa_json):
    mapa = photos_linker.carregar_mapa_imagens(mapa_json)
    assert set(mapa) == {"CA18CBF08BPA2IO", "RM1D060D10"}


def test_dry_run_nao_chama_api(mapa_json, nuvemshop_mock):
    res = photos_linker.vincular_fotos_gavazzi(mapa_json, dry_run=True)
    assert res == {"sucessos": 0, "ignorados": 0, "falhas": 0}
    assert nuvemshop_mock["uploads"] == []


def test_vincula_produto_sem_imagem(mapa_json, nuvemshop_mock, capsys):
    res = photos_linker.vincular_fotos_gavazzi(mapa_json)
    # CA18 recebeu 2 fotos; RM1D não existe na loja → falha
    assert res["sucessos"] == 1
    assert res["falhas"] == 1
    assert [u for u, _ in nuvemshop_mock["uploads"]] == [11, 11]
    assert "CA18CBF08BPA2IO" in nuvemshop_mock["registrados"]
    assert "Vinculação de fotos concluída" in capsys.readouterr().out


def test_produto_com_imagem_ignorado(mapa_json, nuvemshop_mock):
    mapa_json.write_text(json.dumps({"COMFOTO": ["https://img/x.png"]}))
    res = photos_linker.vincular_fotos_gavazzi(mapa_json)
    assert {k: res[k] for k in ("sucessos", "ignorados", "falhas")} == \
        {"sucessos": 0, "ignorados": 1, "falhas": 0}
    assert nuvemshop_mock["uploads"] == []


def test_force_substitui_imagem(mapa_json, nuvemshop_mock):
    mapa_json.write_text(json.dumps({"COMFOTO": ["https://img/x.png"]}))
    res = photos_linker.vincular_fotos_gavazzi(mapa_json, force=True)
    assert res["sucessos"] == 1
    assert nuvemshop_mock["limpados"] == [22]
    assert nuvemshop_mock["uploads"] == [(22, "https://img/x.png")]


def test_sku_no_cache_ignorado(mapa_json, nuvemshop_mock, monkeypatch):
    monkeypatch.setattr(photos_linker, "carregar_skus_foto_vinculada",
                        lambda: {"CA18CBF08BPA2IO"})
    mapa_json.write_text(json.dumps({"CA18CBF08BPA2IO": ["https://img/x.png"]}))
    res = photos_linker.vincular_fotos_gavazzi(mapa_json)
    assert {k: res[k] for k in ("sucessos", "ignorados", "falhas")} == \
        {"sucessos": 0, "ignorados": 1, "falhas": 0}


def test_validacao_exige_imagens_json(tmp_path, monkeypatch):
    painel = Painel.__new__(Painel)
    monkeypatch.setattr("wago.dashboard.service.OUT", tmp_path)
    with pytest.raises(ErroPainel) as exc:
        painel._parametros_gavazzi_fotos({"confirmar": True})
    assert exc.value.status == 400
    (tmp_path / "gavazzi-imagens.json").write_text("{}")
    params = painel._parametros_gavazzi_fotos({"confirmar": True})
    assert params["mapa"].endswith("gavazzi-imagens.json")


def test_operacao_registrada_na_fila():
    from wago.dashboard.service import ACOES_REAIS, OPERACOES
    assert "gavazzi-vincular-fotos" in OPERACOES
    assert "gavazzi-vincular-fotos" in ACOES_REAIS
