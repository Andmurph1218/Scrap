"""HUD do Motor de Categorias: Aba 3 (organização), Aba 2 (propostas) e ações na fila do painel."""

from __future__ import annotations

import json
import urllib.request

import pytest

from wago.dashboard import taxonomia as tx
from wago.dashboard.server import criar_servidor
from wago.dashboard.service import ACOES_REAIS, ErroPainel, Painel
from wago.dashboard.store import Store
from wago.taxonomia import classificador as cl
from wago.taxonomia import modelo


def _no(i, nome, pai=None, cid=None):
    n = modelo._no(i, nome, pai)
    n["nuvemshop_id"] = cid
    return n


TAX = {"status": modelo.STATUS_APROVADA, "versao": 2, "aprovado_por": "Andre", "aprovado_em": "2026-11-04T10:00:00",
       "fora_do_ramo": ["Utensílios de Cozinha"],
       "nos": [_no("materiais", "Materiais Elétricos", cid=1), _no("reles", "Relés", "materiais", 2),
               _no("disjuntor", "Disjuntor", "materiais", 3), _no("infra", "Infraestrutura de Painel", cid=4),
               _no("canaletas", "Canaletas e Acessórios", "infra")],
       "mapa_categorias_loja": {}}


def _r(pid, nome, status, **kw):
    return {"produto_id": pid, "nome": nome, "skus": [f"SKU{pid}"], "status": status, "origem": kw.pop("origem", "regras"),
            "confianca": kw.pop("confianca", 0.5), "categoria_id": kw.pop("categoria_id", None),
            "motivos_revisao": ["confiança abaixo"], **kw}


CLASSIF = {
    "gerada_em": "2026-11-04T10:30:00", "taxonomia_versao": 2, "parcial": False,
    "resumo": {"total": 6, "categorizados_auto": 1},
    "resultados": [
        _r(1, "Rele 24V", cl.STATUS_AUTO, confianca=0.9, categoria_id="reles", categoria_nome="Relés"),
        _r(2, "Quadro Distribuição 12 DIN", cl.STATUS_REVISAO, confianca=0.6,
           sugestoes=[{"categoria_id": "disjuntor", "categoria_nome": "Disjuntor", "score": 0.6}],
           nova_categoria={"nome": "Quadros de Distribuição", "pai": "infra"}),
        _r(3, "Quadro de Comando 40x30", cl.STATUS_REVISAO, confianca=0.4, origem="ia",
           nova_categoria={"nome": "Quadro de Distribuição", "pai": "infra"}),
        _r(4, "Espremedor de Alho Inox", cl.STATUS_REVISAO, confianca=0.0, fora_do_ramo=True,
           fora_do_ramo_motivo="espremedor", nova_categoria={"nome": "Utensílios de Cozinha", "pai": None}),
        _r(5, "Mata Junta Canaleta", cl.STATUS_REVISAO, confianca=0.7,
           sugestoes=[{"categoria_id": "canaletas", "categoria_nome": "Canaletas e Acessórios", "score": 0.7}],
           nova_categoria={"nome": "Canaletas", "pai": "infra"}),
        _r(6, "Coisa estranha", cl.STATUS_REVISAO, confianca=0.0),
    ],
}


@pytest.fixture
def pasta(tmp_path, monkeypatch):
    monkeypatch.setattr(modelo, "TAXONOMIA", tmp_path)
    monkeypatch.setattr(cl, "TAXONOMIA", tmp_path)
    (tmp_path / "taxonomia.json").write_text(json.dumps(TAX), encoding="utf-8")
    dados = {**CLASSIF, "propostas": cl.agrupar_propostas(CLASSIF["resultados"])}
    (tmp_path / "classificacao-20261104T103000.json").write_text(json.dumps(dados), encoding="utf-8")
    tx._cache.update(caminho=None, mtime=0.0, dados=None)
    return tmp_path


def _painel(tmp_path):
    return Painel(Store(tmp_path / "dash.sqlite"), ACOES_REAIS)


def _rodar(painel, corpo):
    ident = painel.iniciar({"confirmar": True, **corpo})
    painel._thread.join(10)
    return painel.store.obter_execucao(ident["id"])


def test_pendentes_filtros_facetas_e_paginacao(pasta):
    d = tx.pendentes({"status": "pendente"})
    assert d["total"] == 5 and d["facetas"]["fora_do_ramo"] == 1
    assert {i["id"] for i in d["itens"]} == {2, 3, 4, 5, 6}
    assert tx.pendentes({"status": "fora_do_ramo"})["itens"][0]["fora_do_ramo_motivo"] == "espremedor"
    assert [i["id"] for i in tx.pendentes({"origem": "ia"})["itens"]] == [3]
    assert {i["id"] for i in tx.pendentes({"conf_min": "0.5", "conf_max": "0.7"})["itens"]} == {2, 5}
    assert [i["id"] for i in tx.pendentes({"busca": "sku5"})["itens"]] == [5]
    pag = tx.pendentes({"por_pagina": "10", "pagina": "9"})
    assert pag["pagina"] == 1 and pag["paginas"] == 1


def test_categorias_finais_excluem_pais_e_incluem_novas(pasta):
    ids = {c["id"] for c in tx.categorias_finais()}
    assert ids == {"reles", "disjuntor", "canaletas"}
    assert next(c for c in tx.categorias_finais() if c["id"] == "canaletas")["nova"] is True


def test_propostas_agrupadas_com_volume_existente_e_fora_do_ramo(pasta):
    p = {x["nome"]: x for x in tx.propostas("todas")["propostas"]}
    quadro = p["Quadros de Distribuição"]
    assert quadro["produtos"] == 2 and set(quadro["variantes"]) == {"Quadros de Distribuição", "Quadro de Distribuição"}
    assert quadro["pai_nome"] == "Infraestrutura de Painel"
    assert p["Utensílios de Cozinha"]["fora_do_ramo"] is True
    assert p["Canaletas"]["existente"] is None  # nome diferente de "Canaletas e Acessórios"


def test_fila_atribuir_em_massa_e_desfazer(pasta, tmp_path):
    painel = _painel(tmp_path)
    ex = _rodar(painel, {"operacao": "tax-atribuir", "ids": [2, 6], "categoria_id": "disjuntor"})
    assert ex["status"] == "concluida" and ex["registros"] == 2
    assert tx.decisoes() == {"2": "disjuntor", "6": "disjuntor"}
    assert {i["id"] for i in tx.pendentes({"status": "decidido"})["itens"]} == {2, 6}
    assert tx.pendentes({"status": "pendente"})["total"] == 3
    _rodar(painel, {"operacao": "tax-desfazer", "ids": [6]})
    assert tx.decisoes() == {"2": "disjuntor"}


def test_fila_aceitar_sugestao_e_todos_filtrados(pasta, tmp_path):
    painel = _painel(tmp_path)
    ex = _rodar(painel, {"operacao": "tax-atribuir", "usar_sugestao": True,
                         "filtro": {"status": "pendente", "conf_min": "0.6"}})
    assert ex["registros"] == 2  # produtos 2 e 5 têm sugestão; 3 não tem
    assert tx.decisoes() == {"2": "disjuntor", "5": "canaletas"}


def test_fila_arquivar_fora_do_ramo(pasta, tmp_path):
    painel = _painel(tmp_path)
    _rodar(painel, {"operacao": "tax-arquivar", "filtro": {"status": "fora_do_ramo"}, "marcar": True})
    assert tx.arquivar()["4"]["motivo"] == "espremedor"
    assert [i["id"] for i in tx.pendentes({"status": "arquivar"})["itens"]] == [4]
    _rodar(painel, {"operacao": "tax-arquivar", "ids": [4], "marcar": False})
    assert tx.arquivar() == {}


def test_validacao_sincrona_antes_de_enfileirar(pasta, tmp_path):
    painel = _painel(tmp_path)
    with pytest.raises(ErroPainel, match="categoria final"):
        painel.iniciar({"confirmar": True, "operacao": "tax-atribuir", "ids": [2], "categoria_id": "materiais"})
    with pytest.raises(ErroPainel, match="ao menos um"):
        painel.iniciar({"confirmar": True, "operacao": "tax-arquivar", "ids": []})
    with pytest.raises(ErroPainel):
        painel.iniciar({"confirmar": True, "operacao": "tax-proposta", "chave": "x", "decisao": "talvez"})


def test_aprovar_proposta_cria_no_pendente_e_decisoes(pasta, tmp_path):
    painel = _painel(tmp_path)
    chave = next(p["chave"] for p in tx.propostas()["propostas"] if p["nome"] == "Quadros de Distribuição")
    ex = _rodar(painel, {"operacao": "tax-proposta", "chave": chave, "decisao": "aprovar",
                         "nome": "Quadros de Distribuição e Comando", "pai": "infra"})
    assert ex["status"] == "concluida"
    st = modelo.status_taxonomia()
    assert st["proposta_pendente"] and not st["liberado_para_classificacao"]
    novo = next(n for n in modelo.carregar_proposta()["nos"] if n["nome"] == "Quadros de Distribuição e Comando")
    assert novo["pai"] == "infra" and "Quadro de Distribuição" in novo["sinonimos"]
    assert tx.decisoes() == {"2": novo["id_alvo"], "3": novo["id_alvo"]}
    assert tx.propostas("aprovada")["propostas"][0]["id_alvo"] == novo["id_alvo"]
    assert any(c["id"] == novo["id_alvo"] for c in tx.categorias_finais())

    _rodar(painel, {"operacao": "tax-aprovar-taxonomia", "por": "Andre"})
    assert modelo.status_taxonomia()["versao"] == 3


def test_aprovar_proposta_como_existente_e_rejeitar(pasta, tmp_path):
    painel = _painel(tmp_path)
    props = {p["nome"]: p["chave"] for p in tx.propostas()["propostas"]}
    _rodar(painel, {"operacao": "tax-proposta", "chave": props["Canaletas"], "decisao": "aprovar", "existente": "canaletas"})
    assert tx.decisoes() == {"5": "canaletas"} and not modelo.status_taxonomia()["proposta_pendente"]
    _rodar(painel, {"operacao": "tax-proposta", "chave": props["Utensílios de Cozinha"], "decisao": "rejeitar"})
    assert tx.propostas_status()[props["Utensílios de Cozinha"]]["status"] == "rejeitada"
    assert props["Utensílios de Cozinha"] not in {p["chave"] for p in tx.propostas("pendente")["propostas"]}


def test_status_resume_classificacao_e_aviso_de_cota(pasta):
    tx.ESTADO_IA["aviso"] = {"provedor": "gemini", "tipo": "por_minuto", "retoma_em": "2026-11-04T10:31:00"}
    try:
        s = tx.status({"id": "abc", "etapa": "tax-classificar"})
        assert s["aviso_ia"]["provedor"] == "gemini"
        assert tx.status(None)["aviso_ia"] is None
    finally:
        tx.ESTADO_IA["aviso"] = None
    c = s["classificacao"]
    assert c["pendentes"] == 5 and c["fora_do_ramo"] == 1 and c["auto"] == 1 and not c["desatualizada"]


def test_rotas_http_da_taxonomia(pasta, tmp_path):
    painel = _painel(tmp_path)
    httpd = criar_servidor(painel, 0)
    import threading

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        for rota in ("status", "pendentes?status=fora_do_ramo", "categorias", "propostas?status=todas"):
            with urllib.request.urlopen(f"{base}/api/taxonomia/{rota}") as resp:
                assert resp.status == 200 and json.loads(resp.read())
        html = urllib.request.urlopen(base + "/").read().decode("utf-8")
        assert "Motor de Categorias" in html and "Marcar para Arquivar (Fase 4)" in html
    finally:
        httpd.shutdown()
        httpd.server_close()
