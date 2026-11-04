import json

import pytest

from wago.taxonomia import inventario
from wago.taxonomia.nuvemshop_api import Cancelado, ErroNuvemshop, NuvemshopAPI, texto_i18n
from wago.taxonomia.texto import normalizar_nome


class Resp:
    def __init__(self, status, corpo=None, headers=None):
        self.status_code = status
        self._corpo = corpo
        self.headers = headers or {}
        self.content = b"x" if corpo is not None else b""
        self.text = json.dumps(corpo)

    def json(self):
        return self._corpo


class FakeSession:
    def __init__(self, rotas):
        self.rotas = rotas
        self.chamadas = []

    def request(self, metodo, url, headers=None, params=None, json=None, timeout=None):
        self.chamadas.append((metodo, url, dict(params or {})))
        chave = (metodo, url.split("/v1/123", 1)[1], (params or {}).get("page"))
        resp = self.rotas[chave]
        return resp.pop(0) if isinstance(resp, list) else resp


def _api(rotas, **kw):
    return NuvemshopAPI("123", "tok", session=FakeSession(rotas), dormir=lambda s: None, **kw)


CATEGORIAS = [
    {"id": 1, "name": {"pt": "Conectores"}, "handle": {"pt": "conectores"}, "parent": None, "subcategories": [2, 3]},
    {"id": 2, "name": {"pt": "Bornes de Passagem"}, "handle": {"pt": "bornes"}, "parent": 1, "subcategories": [4]},
    {"id": 3, "name": {"pt": "Borne de passagem"}, "handle": {"pt": "borne-2"}, "parent": 1, "subcategories": []},
    {"id": 4, "name": {"pt": "Nível 3"}, "handle": {"pt": "n3"}, "parent": 2, "subcategories": []},
    {"id": 5, "name": {"pt": "Página inicial"}, "handle": {"pt": "home"}, "parent": 0, "subcategories": []},
    {"id": 6, "name": {"pt": "2002-1201 TOPJOB S"}, "handle": {"pt": "sku"}, "parent": None, "subcategories": []},
    {"id": 7, "name": {"pt": "Rail-mounted terminal block"}, "handle": {"pt": "en"}, "parent": None, "subcategories": []},
]
PRODUTOS = [
    {"id": 10, "name": {"pt": "Borne A"}, "categories": [{"id": 2}], "variants": [{"sku": "2002-1201"}]},
    {"id": 11, "name": {"pt": "Borne B"}, "categories": [{"id": 3}, {"id": 6}], "variants": [{"sku": "X1"}]},
    {"id": 12, "name": {"pt": "Solto"}, "categories": [], "variants": [{"sku": ""}]},
    {"id": 13, "name": {"pt": "Pai"}, "categories": [{"id": 1}, {"id": 2}], "variants": []},
]


def test_texto_i18n_e_normalizacao():
    assert texto_i18n({"es": "Hola", "pt": "Olá"}) == "Olá"
    assert texto_i18n("x") == "x"
    assert normalizar_nome("Bornes de Passagem") == normalizar_nome("borne de passagem")
    assert normalizar_nome("Conexões") == normalizar_nome("conexao")
    assert "possível nome em inglês" in inventario.motivos_nome_suspeito("Interface Modules and System Wiring")
    assert "possível nome em inglês" in inventario.motivos_nome_suspeito("Energy Measurement Technology")
    assert inventario.motivos_nome_suspeito("Módulos de relés e acopladores óticos") == []


def test_paginacao_para_no_404_last_page_e_pagina_incompleta():
    pag1 = [{"id": i} for i in range(200)]
    api = _api({
        ("GET", "/categories", 1): Resp(200, pag1),
        ("GET", "/categories", 2): Resp(404, {"description": "Last page is 1"}),
    })
    assert len(api.listar_categorias()) == 200

    api = _api({("GET", "/categories", 1): Resp(200, [{"id": 1}])})
    assert len(api.listar_categorias()) == 1


def test_retry_em_429_e_5xx_e_erro_em_4xx():
    api = _api({("GET", "/categories/9", None): [
        Resp(429, {}, {"x-rate-limit-reset": "10"}), Resp(502, {}), Resp(200, {"id": 9}),
    ]})
    assert api.obter_categoria(9) == {"id": 9}
    assert api.requisicoes == 3

    api = _api({("GET", "/categories/9", None): Resp(401, {"description": "Invalid token"})})
    with pytest.raises(ErroNuvemshop) as exc:
        api.obter_categoria(9)
    assert exc.value.status == 401


def test_cancelamento_cooperativo():
    api = _api({}, cancelado=lambda: True)
    with pytest.raises(Cancelado):
        api.listar_categorias()


def test_snapshot_preserva_parent_id_e_dados_brutos(tmp_path):
    api = _api({
        ("GET", "/categories", 1): Resp(200, CATEGORIAS),
        ("GET", "/products", 1): Resp(200, PRODUTOS),
    })
    snap = inventario.gerar_snapshot(api, log=lambda m: None)
    por_id = {c["id"]: c for c in snap["categorias"]}
    assert por_id[2]["parent_id"] == 1
    assert por_id[4]["parent_id"] == 2
    assert por_id[5]["parent_id"] is None  # parent 0 = raiz
    assert por_id[2]["bruto"]["handle"] == {"pt": "bornes"}
    assert snap["produtos"][1]["categorias"] == [3, 6]

    caminho = inventario.salvar_snapshot(snap, tmp_path / "s.json")
    assert inventario.carregar_snapshot(caminho)["categorias"][1]["parent_id"] == 1


def test_diagnostico_detecta_problemas():
    api = _api({
        ("GET", "/categories", 1): Resp(200, CATEGORIAS),
        ("GET", "/products", 1): Resp(200, PRODUTOS),
    })
    snap = inventario.gerar_snapshot(api, log=lambda m: None)
    diag = inventario.diagnosticar(snap, {"2002-1201"})
    t = diag["totais"]

    assert t["categorias"] == 7 and t["produtos"] == 4
    assert t["produtos_fonte_wago"] == 1
    assert {c["id"] for c in diag["vazias"]} == {4, 5, 7}
    assert [[c["id"] for c in g["categorias"]] for g in diag["duplicadas"]] == [[2, 3]]
    motivos = {c["id"]: c["motivos"] for c in diag["nomes_suspeitos"]}
    assert "fragmento de breadcrumb" in motivos[5]
    assert "contém código/SKU" in motivos[6]
    assert "possível nome em inglês" in motivos[7]
    assert [c["id"] for c in diag["nivel_excedente"]] == [4]
    assert t["produtos_sem_categoria"] == 1
    assert [p["id"] for p in diag["produtos_multiplas_categorias_folha"]] == [11]
    # produto 13 está em 1 (pai) e 2 (filha): só 2 conta como folha, mas 2 tem filha (nível 3)
    assert 13 in [p["id"] for p in diag["produtos_em_categoria_nao_folha"]]

    md = inventario.relatorio_markdown(diag)
    assert "Categorias vazias" in md and "Duplicadas" in md


def test_executar_inventario_grava_arquivos(tmp_path, monkeypatch):
    monkeypatch.setattr(inventario, "TAXONOMIA", tmp_path)
    monkeypatch.setattr(inventario, "TAXONOMIA_SNAPSHOTS", tmp_path / "snapshots")
    monkeypatch.setattr(inventario, "ensure_dirs", lambda: None)
    monkeypatch.setattr(inventario, "skus_wago", lambda: set())
    api = _api({
        ("GET", "/categories", 1): Resp(200, CATEGORIAS),
        ("GET", "/products", 1): Resp(200, PRODUTOS),
    })
    saida = inventario.executar_inventario(api=api, log=lambda m: None)
    assert saida["snapshot"].exists()
    assert json.loads(saida["diagnostico_json"].read_text(encoding="utf-8"))["totais"]["categorias"] == 7
    assert saida["diagnostico_md"].read_text(encoding="utf-8").startswith("# Diagnóstico")
