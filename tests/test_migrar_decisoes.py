"""Migração das decisões humanas: backup + caminho estável da folha."""

import json

from wago.taxonomia import classificador as cl
from wago.taxonomia import modelo
from wago.taxonomia.migrar_decisoes import migrar


def test_migrar_faz_backup_e_grava_o_caminho(tmp_path):
    tax = {"nos": [
        modelo._no("mat", "Materiais Elétricos", None),
        modelo._no("inst", "Instalação", "mat"),
        modelo._no("bornes", "Bornes", "inst"),
    ]}
    (tmp_path / "taxonomia.json").write_text(json.dumps(tax), encoding="utf-8")
    (tmp_path / "decisoes.json").write_text(json.dumps({"9": "bornes", "10": "nao-existe"}), encoding="utf-8")

    r = migrar(tmp_path)
    assert r["migrados"] == 2 and r["aviso"] is None
    backup = json.loads(open(r["backup"], encoding="utf-8").read())
    assert backup == {"9": "bornes", "10": "nao-existe"}
    novo = json.loads((tmp_path / "decisoes.json").read_text(encoding="utf-8"))
    assert novo["9"] == {"id_alvo": "bornes", "caminho": "material eletrico > instalacao > borne"}
    assert novo["10"] == {"id_alvo": "nao-existe", "caminho": ""}


def test_migrar_sem_arquivo_nao_inventa_dados(tmp_path):
    r = migrar(tmp_path)
    assert r["migrados"] == 0 and "não existe" in r["aviso"]
    assert not (tmp_path / "decisoes.json").exists()


def test_decisao_resolve_pelo_caminho_quando_o_id_some(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "TAXONOMIA", tmp_path)
    monkeypatch.setattr(modelo, "TAXONOMIA", tmp_path)
    tax = {"status": modelo.STATUS_APROVADA, "nos": [
        modelo._no("mat", "Materiais Elétricos", None),
        modelo._no("bornes-novos", "Bornes", "mat"),
    ]}
    (tmp_path / "taxonomia.json").write_text(json.dumps(tax), encoding="utf-8")
    (tmp_path / "decisoes.json").write_text(json.dumps({
        "9": {"id_alvo": "bornes-antigo", "caminho": "material eletrico > borne"},
    }), encoding="utf-8")
    assert cl.carregar_decisoes() == {"9": "bornes-novos"}
