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
    funil,
    redigir,
)
from wago.dashboard.store import Store  # noqa: E402
from wago.images import baixar_imagens_lote  # noqa: E402


def test_funil_conta_cada_sku_pelo_status_do_catalogo(_catalogo_isolado):
    from wago.state import (
        catalogo,
        registrar_falha_scrape,
        registrar_sku_processado,
        registrar_sku_sem_preco,
        sincronizar_saidas,
    )

    out_dir = _catalogo_isolado / "out"
    base = "https://www.wago.com/br/p/"
    cat = catalogo()
    cat.registrar_descobertos(
        [base + s for s in ("209-659", "770-659", "999-999", "111-111", "222-222", "209-659")]
    )
    cat.enfileirar()
    registrar_sku_processado("209-659")
    registrar_sku_processado("770-659")
    registrar_sku_sem_preco("999-999")
    registrar_falha_scrape("111-111", "IPI indisponível")

    out_dir.mkdir(exist_ok=True)
    (out_dir / "produtos-Nuvemshop-preenchido.csv").write_text(
        "SKU;Nome\n209-659;A\n770-659;B\n", encoding="utf-8-sig"
    )
    fotos = out_dir / "imagens" / "209-659__a"
    fotos.mkdir(parents=True)
    (fotos / "01.jpg").write_bytes(b"x")
    (out_dir / "imagens" / "770-659__b").mkdir()
    sincronizar_saidas()

    f = funil()
    assert f["total"] == 5
    assert f["AGUARDANDO_SCRAPE"] == 2
    assert f["com_falha"] == 1
    assert f["FILTRADO_SEM_CUSTO"] == 1
    assert f["EXPORTADO"] == 1
    assert f["AGUARDANDO_IMAGEM"] == 1
    assert f["SCRAPE_OK"] == 0
    assert f["processados"] == 2
    assert f["no_csv"] == 2


def test_lista_de_arquivos_mostra_so_entregaveis(tmp_path, monkeypatch):
    banco = tmp_path / "d.sqlite"
    tmp_path = tmp_path / "out"
    tmp_path.mkdir()
    monkeypatch.setattr("wago.dashboard.service.OUT", tmp_path)
    (tmp_path / ".gitkeep").write_text("")
    (tmp_path / "Anunciar-ML-preenchido.xlsx").write_bytes(b"x")
    (tmp_path / "Anunciar-ML-preenchido_28-10_145612.xlsx").write_bytes(b"x")
    (tmp_path / "Anunciar-ML-preenchido_28-10_145612.resumo.json").write_text(
        '{"exportados": 510}', encoding="utf-8"
    )
    (tmp_path / "produtos-Nuvemshop-preenchido.csv").write_text(
        "SKU;Nome\n1;A\n2;B\n", encoding="utf-8-sig"
    )
    (tmp_path / "produtos-Nuvemshop-preenchido_28-10_145500.csv").write_text("SKU\n1\n")
    (tmp_path / "wago lote 1 28-10 master.csv").write_text("SKU\n1\n2\n3\n")
    (tmp_path / "wago_nuvemshop_lote_latest.csv").write_text("SKU\n1\n")
    for sku in ("2002-2201__borne", "281-354__placa"):
        pasta = tmp_path / "imagens" / sku
        pasta.mkdir(parents=True)
        (pasta / "01.jpg").write_bytes(b"12345")
        (pasta / "02.jpg").write_bytes(b"12345")
    (tmp_path / "imagens" / "resumo-imagens.json").write_text("{}")

    itens = {item["nome"]: item for item in Painel(Store(banco)).arquivos()}
    assert set(itens) == {
        "Anunciar-ML-preenchido.xlsx",
        "produtos-Nuvemshop-preenchido.csv",
        "wago lote 1 28-10 master.csv",
        "imagens/",
    }
    assert itens["Anunciar-ML-preenchido.xlsx"]["produtos"] == 510
    assert itens["produtos-Nuvemshop-preenchido.csv"]["produtos"] == 2
    assert itens["imagens/"]["tipo"] == "pasta"
    assert itens["imagens/"]["produtos"] == 2
    assert itens["imagens/"]["arquivos"] == 5


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


def test_resetar_contadores_e_modo_continuacao(tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    state_dir = tmp_path / "state"
    raw_dir = tmp_path / "raw"
    out_dir.mkdir()
    state_dir.mkdir()
    raw_dir.mkdir()
    skus_proc = state_dir / "skus_processados.txt"
    skus_sem = state_dir / "skus_sem_preco.txt"
    links_file = raw_dir / "links_wago_coletados.json"
    ckpt = state_dir / "checkpoint_master_lote_1.csv"

    monkeypatch.setattr("wago.dashboard.service.OUT", out_dir)
    monkeypatch.setattr("wago.dashboard.service.STATE", state_dir)
    monkeypatch.setattr("wago.dashboard.service.SKUS_PROCESSADOS", skus_proc)
    monkeypatch.setattr("wago.dashboard.service.SKUS_SEM_PRECO", skus_sem)
    monkeypatch.setattr("wago.dashboard.service.LINKS_FILE", links_file)
    monkeypatch.setattr("wago.state.SKUS_PROCESSADOS", skus_proc)
    monkeypatch.setattr("wago.state.SKUS_SEM_PRECO", skus_sem)
    monkeypatch.setattr("wago.state.LINKS_FILE", links_file)
    monkeypatch.setattr("wago.config.CATALOGO", state_dir / "catalogo.sqlite")

    # Estado legado em texto: importado na primeira abertura do catálogo.
    skus_proc.write_text("209-659\n770-659\n", encoding="utf-8")
    skus_sem.write_text("999-999\n", encoding="utf-8")
    links_file.write_text('["https://www.wago.com/br/p/209-659"]', encoding="utf-8")
    ckpt.write_text("SKU;Nome\n209-659;Cartão\n", encoding="utf-8")
    (out_dir / "produtos-Nuvemshop-preenchido.csv").write_text(
        "SKU;Nome\n209-659;Cartão\n", encoding="utf-8-sig"
    )
    (out_dir / ".gitkeep").write_text("", encoding="utf-8")

    store = Store(state_dir / "dash.sqlite")
    painel = Painel(store, {"discover": lambda p, c: 1})
    ident = painel.iniciar({"operacao": "discover", "confirmar": True})
    assert painel._thread is not None
    painel._thread.join(3)

    antes = painel.painel()
    assert antes["modo_continuacao"] is True
    assert antes["indicadores"]["processados"] == 2
    assert antes["indicadores"]["falhas"] == 1
    assert antes["indicadores"]["exportados"] == 1
    assert antes["indicadores"]["descobertos"] == 3
    assert antes["indicadores"]["execucoes_concluidas"] == 1

    with pytest.raises(ErroPainel) as erro:
        painel.resetar({})
    assert erro.value.status == 400

    res = painel.resetar({"confirmar": True})
    assert res["ok"] is True
    depois = res["painel"]
    assert depois["modo_continuacao"] is False
    assert depois["indicadores"] == {
        "descobertos": 0,
        "processados": 0,
        "falhas": 0,
        "exportados": 0,
        "imagens": 0,
        "execucoes_concluidas": 0,
        "execucoes_falhas": 0,
    }
    assert not ckpt.exists()
    assert not skus_proc.exists()
    assert (out_dir / ".gitkeep").exists()
    assert store.obter_execucao(ident["id"]) is None


def test_etapa_photos_no_painel_e_envio_base64(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from wago.extract import filtrar_melhores_fotos
    from wago.photos import vincular_fotos_lote

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setattr("wago.dashboard.service.OUT", out_dir)
    monkeypatch.setattr("wago.photos.OUT", out_dir)
    monkeypatch.setenv("NUVEMSHOP_STORE_ID", "999")
    monkeypatch.setenv("NUVEMSHOP_ACCESS_TOKEN", "tok123")

    # Filtra miniaturas 120px, ícones 0008.png e mantém apenas a maior resolução de cada imagem
    urls_mistas = [
        "https://www.wago.com/medias/1024-020000030002f8f9000200b6-DE.jpg?context=abc",
        "https://www.wago.com/medias/2000-020000030002f8f9000200b6-DE.jpg?context=def",
        "https://www.wago.com/medias/0008.png?context=ico",
        "https://www.wago.com/medias/120-020000030002f8f9000200b6-DE.jpg?context=thumb",
    ]
    assert filtrar_melhores_fotos(urls_mistas) == [
        "https://www.wago.com/medias/2000-020000030002f8f9000200b6-DE.jpg?context=def"
    ]

    master = out_dir / "wago lote 1 26-10 master.csv"
    master.write_text(
        "SKU;Nome;Custo;Preço;IPI;Fotos\n"
        "209-659;Cartão WSB;62.86;114.98;6.13;"
        "https://www.wago.com/medias/1024-imgA-DE.jpg, "
        "https://www.wago.com/medias/0008.png, "
        "https://www.wago.com/medias/120-imgA-DE.jpg, "
        "https://www.wago.com/medias/1024-imgB-DE.jpg\n",
        encoding="utf-8-sig",
    )

    painel = Painel(Store(tmp_path / "dash.sqlite"))
    ids_etapas = [c["id"] for c in painel.painel()["etapas"]]
    assert "photos" in ids_etapas

    # Dry-run conta apenas as 2 fotos de alta qualidade (ignora 0008.png e 120-imgA)
    assert vincular_fotos_lote(master, dry_run=True) == 2

    def fake_get(url, headers=None, timeout=30):
        resp = MagicMock()
        resp.status_code = 200
        if "products?per_page=200&page=1" in url:
            resp.json.return_value = [
                {"id": 555, "variants": [{"sku": "209-659"}]}
            ]
        elif "products?per_page=200&page=2" in url:
            resp.json.return_value = []
        elif url.endswith("/products/555"):
            resp.json.return_value = {"id": 555, "images": []}
        elif "1024-imgA" in url:
            resp.content = b"\xff\xd8\xff\xe0JFIF_IMAGEM_A_ALTA"
        elif "1024-imgB" in url:
            resp.content = b"\xff\xd8\xff\xe0JFIF_IMAGEM_B_ALTA"
        return resp

    post_calls = []

    def fake_post(url, headers=None, json=None, timeout=60):
        post_calls.append((url, json))
        resp = MagicMock()
        resp.status_code = 201
        return resp

    monkeypatch.setattr("wago.photos.requests.get", fake_get)
    monkeypatch.setattr("wago.photos.requests.post", fake_post)
    monkeypatch.setattr("wago.photos.time.sleep", lambda s: None)

    enviadas = vincular_fotos_lote(master)
    assert enviadas == 2
    assert len(post_calls) == 2
    assert post_calls[0][0].endswith("/v1/999/products/555/images")
    assert "attachment" in post_calls[0][1]
    assert post_calls[0][1]["filename"] == "1024-imgA-DE.jpg"
    assert post_calls[1][1]["filename"] == "1024-imgB-DE.jpg"
    assert (out_dir / "resumo-fotos-nuvemshop.json").exists()
