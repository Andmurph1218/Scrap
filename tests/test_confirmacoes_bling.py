import json

import pytest

from wago.taxonomia import modelo
from wago.dashboard import taxonomia as tx


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setattr(modelo, "TAXONOMIA", tmp_path)
    monkeypatch.setattr(tx, "_itens_bloqueados_bling", lambda: [])
    monkeypatch.setattr(tx, "_itens_revisao_bling", lambda: [{"id": 1, "status": "revisao"}, {"id": 2, "status": "revisao"}])
    return tmp_path


def test_confirmacao_persistente_remove_da_revisao_e_deduplica(ambiente):
    from wago.taxonomia.confirmacoes_bling import confirmar, confirmados

    confirmar(1, [10])
    confirmar(1, [10])
    assert set(confirmados()) == {1}
    assert [i["id"] for i in tx.pendentes_bling()["itens"]] == [2]
    assert tx.bling_ids_por_filtro({}) == [2]


def test_reconciliacao_so_reabre_categoria_divergente(ambiente):
    from wago.taxonomia.confirmacoes_bling import confirmar, confirmados, reconciliar

    confirmar(1, [10])
    confirmar(2, [20])
    reconciliar([{"id": 1, "categorias": [10]}, {"id": 2, "categorias": [30]}])
    assert set(confirmados()) == {1}
    assert [i["id"] for i in tx.pendentes_bling()["itens"]] == [2]


def test_inventario_reabre_produto_que_nao_estava_em_revisao(ambiente, monkeypatch):
    from wago.taxonomia.confirmacoes_bling import confirmar, reconciliar

    monkeypatch.setattr(tx, "_itens_revisao_bling", lambda: [])
    confirmar(99, [10])
    reconciliar([{"id": 99, "nome": "Produto automático", "categorias": []}])
    assert [i["id"] for i in tx.pendentes_bling()["itens"]] == [99]
    reconciliar([{"id": 99, "nome": "Produto automático", "categorias": [10]}])
    assert tx.pendentes_bling()["total"] == 0


def test_contador_produtos_independente_do_filtro(ambiente):
    from wago.taxonomia.confirmacoes_bling import confirmar

    confirmar(1, [10])
    dados = tx.pendentes_bling({"busca": "inexistente"})
    assert dados["aplicacao"] == {"aplicados": 1, "restantes": 1, "total": 2}


def test_sucessos_do_diario_antigo_entram_sem_falhas(ambiente):
    from wago.taxonomia.confirmacoes_bling import confirmados, reconciliar

    (ambiente / "planos").mkdir()
    (ambiente / "aplicacoes").mkdir()
    plano = {"fases": {"mover": [{"seq": 1, "acao": "mover_produto", "produto": 1, "para": [10]}, {"seq": 2, "acao": "mover_produto", "produto": 2, "para": [20]}]}}
    (ambiente / "planos" / "plano.json").write_text(json.dumps(plano))
    diario = {"provider": "bling", "plano": "plano.json", "acoes": [{"seq": 1, "status": "ok"}, {"seq": 2, "status": "falha"}]}
    (ambiente / "aplicacoes" / "aplicacao-bling-teste.json").write_text(json.dumps(diario))
    assert set(confirmados()) == {1}
    reconciliar([{"id": 1, "categorias": []}])
    assert confirmados() == {}
    diario["concluida_em"] = "2026-11-24T20:00:00"
    (ambiente / "aplicacoes" / "aplicacao-bling-teste.json").write_text(json.dumps(diario))
    assert confirmados() == {}


def test_atribuicao_registra_apenas_sucesso(ambiente, monkeypatch):
    from types import SimpleNamespace
    from wago.dashboard.service import _acao_tax_atribuir_bling
    from wago.marketplaces.base import ErroMarketplace
    from wago.taxonomia.confirmacoes_bling import confirmados
    import wago.marketplaces
    import wago.config

    mapping = ambiente / "bling_mapping.json"
    mapping.write_text(json.dumps({"alvo": 10}))
    monkeypatch.setattr(wago.config, "load_env", lambda: None)
    monkeypatch.chdir(ambiente)
    (ambiente / "data/state").mkdir(parents=True)
    (ambiente / "data/state/bling_mapping.json").write_text(mapping.read_text())

    def atualizar(pid, dados):
        if pid == 2:
            raise ErroMarketplace(400, "falhou")

    provider = SimpleNamespace(client=SimpleNamespace(auth=SimpleNamespace(access_token=lambda: "teste")), atualizar_produto=atualizar)
    monkeypatch.setattr(wago.marketplaces, "criar_provider", lambda _: provider)
    assert _acao_tax_atribuir_bling({"ids": [1, 2], "id_alvo": "alvo"}, lambda: False) == 1
    assert confirmados() == {1: [10]}


def test_diario_parcial_nao_quebra_contador(ambiente):
    (ambiente / "aplicacoes").mkdir()
    (ambiente / "aplicacoes/aplicacao-bling-parcial.json").write_text('{"provider":')
    assert tx.pendentes_bling()["aplicacao"]["restantes"] == 2


def test_inventario_incompleto_nao_reabre_confirmacoes(ambiente, monkeypatch):
    from types import SimpleNamespace
    from wago.dashboard.service import ACOES_REAIS
    from wago.taxonomia.confirmacoes_bling import confirmar, confirmados
    from wago.marketplaces.base import ErroRateLimit
    import wago.marketplaces
    import wago.config

    confirmar(1, [10])
    monkeypatch.setattr(wago.config, "load_env", lambda: None)

    def produtos():
        yield {"id": 1, "categorias": []}
        raise ErroRateLimit(429, "limite")

    monkeypatch.setattr(wago.marketplaces, "criar_provider", lambda _: SimpleNamespace(listar_categorias=lambda: [], listar_produtos=produtos))
    with pytest.raises(ErroRateLimit):
        ACOES_REAIS["tax-inventario-bling"]({}, lambda: False)
    assert confirmados() == {1: [10]}
    assert not (ambiente / "normalizados").exists()


def test_inventario_explicito_reconcilia_e_salva_snapshot(ambiente, monkeypatch):
    from types import SimpleNamespace
    from wago.dashboard.service import ACOES_REAIS
    from wago.taxonomia.confirmacoes_bling import confirmar, confirmados
    import wago.marketplaces
    import wago.config

    confirmar(1, [10])
    monkeypatch.setattr(wago.config, "load_env", lambda: None)
    provider = SimpleNamespace(listar_categorias=lambda: [], listar_produtos=lambda: [{"id": 1, "nome": "Teste", "categorias": []}])
    monkeypatch.setattr(wago.marketplaces, "criar_provider", lambda _: provider)
    assert ACOES_REAIS["tax-inventario-bling"]({}, lambda: False) == 1
    assert confirmados() == {}
    assert len(list((ambiente / "normalizados").glob("snapshot-bling-normalizado-*.json"))) == 1


def test_aplicacao_do_plano_confirma_produto(ambiente):
    from types import SimpleNamespace
    from wago.taxonomia.aplicar import _executar_acao_universal
    from wago.taxonomia.confirmacoes_bling import confirmados

    provider = SimpleNamespace(nome="bling", atualizar_produto=lambda *args: None,
                               listar_produtos=lambda: [{"id": 1, "categorias": [10]}])
    _executar_acao_universal(provider, {"acao": "mover_produto", "produto": 1, "para": [10]}, {})
    assert confirmados() == {1: [10]}
