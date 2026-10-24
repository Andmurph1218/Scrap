"""Painel local: validação, concorrência, histórico e arquivos."""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wago.dashboard.server import criar_servidor  # noqa: E402
from wago.dashboard.service import (  # noqa: E402
    ORDEM_PIPELINE,
    ErroPainel,
    Painel,
    redigir,
)
from wago.dashboard.store import Store  # noqa: E402
from wago.images import baixar_imagens_lote  # noqa: E402


def test_systemexit_numerico_nao_vira_log_generico(tmp_path):
    def falha(params, cancelado):
        raise SystemExit(1)

    painel = Painel(Store(tmp_path / "dash.sqlite"), {"discover": falha})
    ident = painel.iniciar({"operacao": "discover", "confirmar": True})
    assert painel._thread is not None
    painel._thread.join(3)
    dados = painel.store.obter_execucao(ident["id"])
    assert dados["status"] == "falha"
    assert dados["mensagem"] != "1"
    assert "código 1" in dados["mensagem"]
    eventos = painel.store.listar_eventos(execucao_id=ident["id"])
    assert eventos[-1]["nivel"] == "erro"
    assert "código 1" in eventos[-1]["mensagem"]


def test_redige_segredo(monkeypatch):
    monkeypatch.setenv("WAGO_PASS", "segredo-teste")
    texto = redigir("Authorization: Bearer abc.def.ghi senha=segredo-teste")
    assert "abc.def.ghi" not in texto
    assert "segredo-teste" not in texto
    assert "[redigido]" in texto


def test_recusa_confirmacao_aba_e_caminho(tmp_path, monkeypatch):
    monkeypatch.setattr("wago.dashboard.service.OUT", tmp_path)
    painel = Painel(Store(tmp_path / "estado" / "dash.sqlite"))
    with pytest.raises(ErroPainel) as erro:
        painel.iniciar({"operacao": "discover"})
    assert erro.value.status == 400
    with pytest.raises(ErroPainel) as erro:
        painel.iniciar(
            {"operacao": "fill-ml", "confirmar": True, "aba": "Aba Inexistente"}
        )
    assert erro.value.status == 400
    with pytest.raises(ErroPainel) as erro:
        painel.iniciar(
            {"operacao": "export", "confirmar": True, "master": "../segredo.csv"}
        )
    assert erro.value.status == 400
    (tmp_path / "ok.csv").write_text("sku\n1\n", encoding="utf-8")
    assert painel.arquivo_autorizado("ok.csv").name == "ok.csv"
    with pytest.raises(ErroPainel):
        painel.arquivo_autorizado("../ok.csv")


def test_uma_execucao_por_vez_e_historico(tmp_path):
    store = Store(tmp_path / "dash.sqlite")
    entrou = threading.Event()

    def lenta(params, cancelado):
        entrou.set()
        while not cancelado():
            threading.Event().wait(0.01)
        print("etapa encerrada apos cancelamento")
        return 2

    painel = Painel(store, {"discover": lenta})
    primeiro = painel.iniciar({"operacao": "discover", "confirmar": True, "limite": 3})
    assert entrou.wait(2)
    with pytest.raises(ErroPainel) as erro:
        painel.iniciar({"operacao": "discover", "confirmar": True})
    assert erro.value.status == 409
    painel.cancelar(primeiro["id"])
    assert painel._thread is not None
    painel._thread.join(3)
    assert store.obter_execucao(primeiro["id"])["status"] == "cancelada"
    reaberto = Store(tmp_path / "dash.sqlite")
    assert reaberto.obter_execucao(primeiro["id"])["codigo"] == 2
    eventos = reaberto.listar_eventos(execucao_id=primeiro["id"])
    assert any("cancelamento" in item["mensagem"].lower() for item in eventos)


def test_pipeline_para_sem_master(tmp_path, monkeypatch):
    monkeypatch.setattr("wago.dashboard.service.OUT", tmp_path / "vazio")
    (tmp_path / "vazio").mkdir()
    chamadas: list[str] = []

    def marcar(nome):
        def _acao(params, cancelado):
            chamadas.append(nome)
            return 1

        return _acao

    acoes = {nome: marcar(nome) for nome in ORDEM_PIPELINE}
    painel = Painel(Store(tmp_path / "dash.sqlite"), acoes)
    ident = painel.iniciar({"operacao": "pipeline", "confirmar": True})
    assert painel._thread is not None
    painel._thread.join(3)
    assert chamadas == ["discover", "scrape"]
    assert painel.store.obter_execucao(ident["id"])["status"] == "falha"


def test_imagens_cancelam_antes_do_download(tmp_path):
    master = tmp_path / "master.csv"
    master.write_text(
        "SKU;Nome;Fotos\n221-413;Conector;https://exemplo.invalid/a.jpg\n",
        encoding="utf-8",
    )
    base = tmp_path / "imagens"
    baixar_imagens_lote(
        master, output_dir=base, dry_run=True, cancelado=lambda: True
    )
    assert not base.exists()


def test_servidor_local(tmp_path, monkeypatch):
    monkeypatch.setattr("wago.dashboard.service.OUT", tmp_path)
    (tmp_path / "nota.csv").write_text("sku\n1\n", encoding="utf-8")
    painel = Painel(Store(tmp_path / "dash.sqlite"), {"discover": lambda p, c: 0})
    httpd = criar_servidor(painel, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    porta = httpd.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{porta}/") as resposta:
            assert b"Wago Pipeline" in resposta.read()
        with urllib.request.urlopen(f"http://127.0.0.1:{porta}/api/painel") as resposta:
            dados = json.loads(resposta.read().decode())
        assert dados["aplicacao"] == "ociosa"
        assert "descobertos" in dados["indicadores"]
        pedido = urllib.request.Request(
            f"http://127.0.0.1:{porta}/api/execucoes",
            data=json.dumps({"operacao": "discover"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as erro:
            urllib.request.urlopen(pedido)
        assert erro.value.code == 400
        with urllib.request.urlopen(
            f"http://127.0.0.1:{porta}/api/arquivos/baixar?nome=nota.csv"
        ) as resposta:
            assert b"sku" in resposta.read()
        with pytest.raises(urllib.error.HTTPError) as erro:
            urllib.request.urlopen(
                f"http://127.0.0.1:{porta}/api/arquivos/baixar?nome=../nota.csv"
            )
        assert erro.value.code == 400
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(3)
