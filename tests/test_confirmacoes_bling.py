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


def test_movimento_usa_apenas_get_put_sem_reler_catalogo(ambiente):
    from wago.marketplaces.bling_provider import BlingProvider, BlingClient
    from wago.marketplaces.rate_limit import RateLimiter
    from wago.taxonomia.aplicar import _executar_acao_universal
    from test_bling_provider import Auth, Sessao, Resp

    sessao = Sessao([Resp(200, {"data": {"id": 1, "nome": "Sensor", "preco": 99, "categoria": {"id": 5}}}), Resp(204)])
    provider = BlingProvider(BlingClient(Auth(), session=sessao, limiter=RateLimiter(dormir=lambda _: None)))
    resultado = _executar_acao_universal(provider, {"acao": "mover_produto", "produto": 1, "para": [10]}, {1: {"categorias": [5]}})
    assert resultado["depois"]["categorias"] == [10]
    assert [(c["metodo"], c["url"].rsplit("/", 2)[-2:]) for c in sessao.chamadas] == [("GET", ["produtos", "1"]), ("PUT", ["produtos", "1"])]
    assert sessao.chamadas[1]["json"]["preco"] == 99


def _aplicar_teste(ambiente, monkeypatch, provider, nome, **kwargs):
    from wago.taxonomia.aplicar import aplicar_acoes_universal

    monkeypatch.setattr(modelo, "carregar_aprovada", lambda: {"nos": []})
    acoes = [{"seq": i, "acao": "mover_produto", "produto": i, "para": [10]} for i in range(1, 5)]
    return aplicar_acoes_universal(provider, acoes, plano_arquivo="plano.json", snapshot_arquivo="snapshot.json",
                                    destino=ambiente / nome, tamanho_lote=2, pausa_lote=30, log=lambda _: None, **kwargs)


def test_micro_lotes_e_retomadas_sucessivas_nao_repetem_sucessos(ambiente, monkeypatch):
    from types import SimpleNamespace
    from wago.marketplaces.base import ErroRateLimit
    from wago.taxonomia.motor_universal import carregar_diario_agnostico
    import wago.marketplaces.rate_limit as rate

    chamadas, pausas = [], []
    rodada = [1]

    def atualizar(pid, dados):
        chamadas.append(pid)
        if (rodada[0], pid) in ((1, 2), (2, 3)):
            raise ErroRateLimit(429, "cota")
        return {"id": pid, "categorias": [10]}

    monkeypatch.setattr(rate.time, "sleep", lambda s: pausas.append(s))
    provider = SimpleNamespace(nome="bling", atualizar_produto=atualizar)
    a = _aplicar_teste(ambiente, monkeypatch, provider, "a.json")
    assert chamadas == [1, 2]
    assert carregar_diario_agnostico(a)["status"] == "pausada"
    rodada[0] = 2
    b = _aplicar_teste(ambiente, monkeypatch, provider, "b.json", retomar=a)
    assert chamadas == [1, 2, 2, 3]
    rodada[0] = 3
    c = _aplicar_teste(ambiente, monkeypatch, provider, "c.json", retomar=b)
    dados = carregar_diario_agnostico(c)
    assert chamadas == [1, 2, 2, 3, 3, 4]
    assert dados["status"] == "concluida"
    assert {a["seq"] for a in dados["acoes"] if a["status"] == "ok"} == {1, 2, 3, 4}


def test_cancelar_durante_pausa_salva_checkpoint_e_reiniciar_pula_sucessos(ambiente, monkeypatch):
    from types import SimpleNamespace
    from wago.taxonomia.motor_universal import carregar_diario_agnostico
    import wago.marketplaces.rate_limit as rate

    chamadas, pausas = [], []
    cancelado = [False]

    def dormir(s):
        pausas.append(s)
        cancelado[0] = True

    def atualizar(pid, dados):
        chamadas.append(pid)
        return {"id": pid, "categorias": [10]}

    monkeypatch.setattr(rate.time, "sleep", dormir)
    provider = SimpleNamespace(nome="bling", atualizar_produto=atualizar)
    a = _aplicar_teste(ambiente, monkeypatch, provider, "a.json", cancelado=lambda: cancelado[0])
    assert carregar_diario_agnostico(a)["status"] == "cancelada"
    assert chamadas == [1, 2]
    assert sum(pausas) <= 0.2
    cancelado[0] = False
    monkeypatch.setattr(rate.time, "sleep", lambda _: None)
    b = _aplicar_teste(ambiente, monkeypatch, provider, "b.json")
    assert chamadas == [1, 2, 3, 4]
    assert carregar_diario_agnostico(b)["status"] == "concluida"


def test_lote_espera_30s_somente_entre_lotes(ambiente, monkeypatch):
    from types import SimpleNamespace
    import wago.marketplaces.rate_limit as rate

    pausas = []
    monkeypatch.setattr(rate.time, "sleep", lambda s: pausas.append(s))
    provider = SimpleNamespace(nome="bling", atualizar_produto=lambda pid, dados: {"id": pid, "categorias": [10]})
    _aplicar_teste(ambiente, monkeypatch, provider, "a.json")
    assert sum(pausas) == pytest.approx(30)


def test_retomada_revalida_categoria_antes_de_excluir(ambiente, monkeypatch):
    from types import SimpleNamespace
    from wago.taxonomia.aplicar import aplicar_acoes_universal
    from wago.taxonomia.motor_universal import DiarioAgnostico, carregar_diario_agnostico

    monkeypatch.setattr(modelo, "carregar_aprovada", lambda: {"nos": []})
    acoes = [{"seq": 1, "acao": "verificar_vazia", "categoria": 10}, {"seq": 2, "acao": "excluir", "categoria": 10}]
    diario = DiarioAgnostico(ambiente / "antes.json", {"provider": "bling", "plano": "p.json"})
    diario.registrar({"seq": 1, "acao": "verificar_vazia", "status": "ok"})
    diario.gravar()
    provider = SimpleNamespace(nome="bling", listar_categorias=lambda: [{"id": 10}], listar_produtos=lambda: [{"id": 1, "categorias": [10]}])
    destino = aplicar_acoes_universal(provider, acoes, plano_arquivo="p.json", snapshot_arquivo="s.json", retomar=diario.caminho, destino=ambiente / "depois.json", log=lambda _: None)
    resultado = carregar_diario_agnostico(destino)
    assert resultado["falhas"] == 1
    assert resultado["puladas"] == 1


def test_plano_alterado_nao_herda_checkpoint_por_seq(ambiente, monkeypatch):
    from types import SimpleNamespace
    from wago.taxonomia.aplicar import aplicar_acoes_universal

    monkeypatch.setattr(modelo, "carregar_aprovada", lambda: {"nos": []})
    aplicados = []

    def atualizar(pid, dados):
        aplicados.append((pid, dados["categorias"]))
        return {"id": pid, **dados}

    provider = SimpleNamespace(nome="bling", atualizar_produto=atualizar)
    a = aplicar_acoes_universal(provider, [{"seq": 1, "acao": "mover_produto", "produto": 1, "para": [10]}], plano_arquivo="p.json", snapshot_arquivo="s.json", destino=ambiente / "a.json")
    aplicar_acoes_universal(provider, [{"seq": 1, "acao": "mover_produto", "produto": 1, "para": [20]}], plano_arquivo="p.json", snapshot_arquivo="s.json", destino=ambiente / "b.json", retomar=a)
    assert aplicados == [(1, [10]), (1, [20])]


def test_aplicacao_do_plano_confirma_produto(ambiente):
    from types import SimpleNamespace
    from wago.taxonomia.aplicar import _executar_acao_universal
    from wago.taxonomia.confirmacoes_bling import confirmados

    provider = SimpleNamespace(nome="bling", atualizar_produto=lambda *args: None,
                               listar_produtos=lambda: [{"id": 1, "categorias": [10]}])
    _executar_acao_universal(provider, {"acao": "mover_produto", "produto": 1, "para": [10]}, {})
    assert confirmados() == {1: [10]}
