"""Catálogo por SKU (máquina de estados), retomada e fila do worker."""

from __future__ import annotations

import sqlite3
import threading

from wago.catalogo import (
    AGUARDANDO_IMAGEM,
    AGUARDANDO_SCRAPE,
    DESCOBERTO,
    EXPORTADO,
    FILTRADO_SEM_CUSTO,
    SCRAPE_OK,
    Catalogo,
)
from wago.dashboard.service import Painel
from wago.dashboard.store import Store
from wago.dashboard.worker import Worker

BASE = "https://www.wago.com/br/p/"


def _links(n: int) -> list[str]:
    return [f"{BASE}{i:03d}-{i:03d}" for i in range(n)]


def test_transicoes_sao_idempotentes_e_nao_regridem(tmp_path):
    cat = Catalogo(tmp_path / "c.sqlite")
    assert cat.registrar_descobertos(_links(3)) == 3
    assert cat.registrar_descobertos(_links(3)) == 0
    assert cat.status_de("000-000") == DESCOBERTO
    assert cat.enfileirar() == 3

    cat.marcar_sem_custo("000-000")
    cat.registrar_falha("000-000", "não deveria valer")
    assert cat.status_de("000-000") == FILTRADO_SEM_CUSTO

    cat.marcar_scrape_ok("001-001", lote=1)
    cat.marcar_scrape_ok("001-001", lote=1)
    cat.registrar_falha("001-001", "não deveria valer")
    assert cat.status_de("001-001") == SCRAPE_OK

    cat.reconciliar_saidas({"001-001"}, set())
    assert cat.status_de("001-001") == AGUARDANDO_IMAGEM
    cat.reconciliar_saidas({"001-001"}, {"001-001"})
    assert cat.status_de("001-001") == EXPORTADO
    cat.marcar_scrape_ok("001-001")
    assert cat.status_de("001-001") == EXPORTADO

    cat.registrar_falha("002-002", "IPI indisponível")
    assert cat.status_de("002-002") == AGUARDANDO_SCRAPE
    assert cat.contagem()["com_falha"] == 1


def test_retomada_continua_do_proximo_item_apos_queda(tmp_path):
    caminho = tmp_path / "c.sqlite"
    cat = Catalogo(caminho)
    cat.registrar_descobertos(_links(448))
    cat.enfileirar()
    ordem_original = [sku for sku, _ in cat.fila_scrape()]

    for sku in ordem_original[:200]:
        cat.marcar_scrape_ok(sku)
    del cat  # o processo "cai" aqui

    reaberto = Catalogo(caminho)
    fila = [sku for sku, _ in reaberto.fila_scrape()]
    assert len(fila) == 248
    assert fila[0] == ordem_original[200]
    assert fila == ordem_original[200:]

    reaberto.registrar_falha(ordem_original[200], "timeout")
    fila = [sku for sku, _ in reaberto.fila_scrape()]
    assert fila[0] == ordem_original[201]
    assert fila[-1] == ordem_original[200]


def test_painel_so_enfileira_e_outro_processo_executa(tmp_path, monkeypatch):
    disparos: list[int] = []
    monkeypatch.setattr(
        "wago.dashboard.service.iniciar_worker_destacado", lambda: disparos.append(1)
    )
    banco = tmp_path / "dash.sqlite"
    painel = Painel(Store(banco))
    ident = painel.iniciar({"operacao": "discover", "confirmar": True})
    assert ident["status"] == "na_fila"
    assert disparos == [1]
    assert painel.painel()["aplicacao"] == "na fila"
    del painel  # o painel fecha; a execução segue na fila em disco

    chamadas: list[dict] = []
    worker = Worker(Store(banco), {"discover": lambda p, c: chamadas.append(p) or 7})
    assert worker.processar_proxima() is True
    assert worker.processar_proxima() is False
    dados = Store(banco).obter_execucao(ident["id"])
    assert dados["status"] == "concluida"
    assert dados["registros"] == 7
    assert chamadas and chamadas[0]["limite"] >= 1


def test_cancelamento_chega_ao_worker_pelo_banco(tmp_path, monkeypatch):
    monkeypatch.setattr("wago.dashboard.service.iniciar_worker_destacado", lambda: None)
    banco = tmp_path / "dash.sqlite"
    painel = Painel(Store(banco))
    entrou = threading.Event()

    def lenta(params, cancelado):
        entrou.set()
        while not cancelado():
            threading.Event().wait(0.01)
        return 1

    ident = painel.iniciar({"operacao": "discover", "confirmar": True})
    worker = Worker(Store(banco), {"discover": lenta})
    fio = threading.Thread(target=worker.processar_proxima)
    fio.start()
    assert entrou.wait(3)
    assert painel.cancelar(ident["id"])["status"] == "cancelando"
    fio.join(5)
    assert Store(banco).obter_execucao(ident["id"])["status"] == "cancelada"


def test_cancelar_na_fila_nao_chega_a_executar(tmp_path, monkeypatch):
    monkeypatch.setattr("wago.dashboard.service.iniciar_worker_destacado", lambda: None)
    banco = tmp_path / "dash.sqlite"
    painel = Painel(Store(banco))
    ident = painel.iniciar({"operacao": "discover", "confirmar": True})
    assert painel.cancelar(ident["id"])["status"] == "cancelada"
    assert Worker(Store(banco), {}).processar_proxima() is False


def test_worker_morto_vira_interrompida_e_libera_a_fila(tmp_path, monkeypatch):
    monkeypatch.setattr("wago.dashboard.service.iniciar_worker_destacado", lambda: None)
    banco = tmp_path / "dash.sqlite"
    store = Store(banco)
    painel = Painel(store)
    ident = painel.iniciar({"operacao": "discover", "confirmar": True})
    assert store.reivindicar(pid=999999) is not None
    with sqlite3.connect(banco) as conn:
        conn.execute(
            "UPDATE execucoes SET batimento='2000-01-01T00:00:00' WHERE id=?",
            (ident["id"],),
        )
    assert painel.painel()["aplicacao"] == "ociosa"
    assert store.obter_execucao(ident["id"])["status"] == "interrompida"
    novo = painel.iniciar({"operacao": "discover", "confirmar": True})
    assert novo["status"] == "na_fila"
