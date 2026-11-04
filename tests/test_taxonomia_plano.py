"""Fase 4 — plano de mudanças completo em dry-run (criar, renomear, reparentar, mover, arquivar, excluir)."""

import json

import pytest

from wago.taxonomia import classificador as cl
from wago.taxonomia import modelo, plano


def _cat(cid, nome, pai=None, handle=None):
    return {"id": cid, "nome": nome, "handle": handle or f"h{cid}", "parent_id": pai, "subcategorias": [], "bruto": {}}


def _no(i, nome, pai=None, cid=None, **kw):
    n = modelo._no(i, nome, pai)
    n.update(nuvemshop_id=cid, **kw)
    return n


SNAP = {
    "store_id": "123", "gerado_em": "2026-11-04T08:00:00",
    "categorias": [
        _cat(10, "Materiais Elétricos"), _cat(11, "Reles", 10), _cat(12, "Disjuntor", 10),
        _cat(20, "Comutação e Potência"), _cat(21, "Contatores", 20),
        _cat(30, "Acessórios", handle="acessorios"), _cat(40, "Vazia"),
    ],
    "produtos": [
        {"id": 1, "nome": "Rele 24V", "categorias": [10, 11], "publicado": True},     # pai+filha → só folha
        {"id": 2, "nome": "Contator 9A", "categorias": [21], "publicado": True},      # filha reparentada: sem mudança
        {"id": 3, "nome": "Cabo PP", "categorias": [30], "publicado": True},          # sai de Acessórios → categoria nova
        {"id": 4, "nome": "Espremedor de Alho", "categorias": [30], "publicado": True},  # arquivar
        {"id": 5, "nome": "Disjuntor 10A", "categorias": [12], "publicado": True},    # já correto
        {"id": 6, "nome": "Item sem destino", "categorias": [20], "publicado": True},  # revisão pendente
        {"id": 7, "nome": "Base Finder 95.05", "categorias": [12], "publicado": True},  # arquivado por engano?
    ],
}
TAX = {
    "versao": 5,
    "nos": [
        _no("materiais", "Materiais Elétricos", cid=10), _no("reles", "Relés", "materiais", 11),
        _no("disjuntor", "Disjuntor", "materiais", 12), _no("contatores", "Contatores", "materiais", 21),
        _no("cabos", "Cabos e Fios", "materiais"),
    ],
    "mesclagens": [{"tipo": "arvore", "origem_nuvemshop_id": 20, "destino_nuvemshop_id": 10}],
    "mapa_categorias_loja": {"30": {"id_alvo": None, "motivo": "categoria genérica removida"},
                             "40": {"id_alvo": None, "motivo": "vazia"}},
}


def _res(pid, status, cat=None, **kw):
    return {"produto_id": pid, "status": status, "categoria_id": cat, "origem": kw.pop("origem", "regras"),
            "confianca": kw.pop("confianca", 0.9), **kw}


CLASSIF = {"_arquivo": "classificacao-x.json", "resultados": [
    _res(1, cl.STATUS_AUTO, "reles", origem="loja"), _res(2, cl.STATUS_AUTO, "contatores", origem="loja"),
    _res(3, cl.STATUS_REVISAO), _res(4, cl.STATUS_REVISAO, fora_do_ramo=True),
    _res(5, cl.STATUS_AUTO, "disjuntor", origem="loja"), _res(6, cl.STATUS_REVISAO),
    _res(7, cl.STATUS_AUTO, "disjuntor", origem="loja"),
]}


@pytest.fixture
def p():
    return plano.gerar_plano(TAX, SNAP, CLASSIF, decisoes={"3": "cabos"},
                             arquivar={"4": {"motivo": "espremedor"}, "7": {"motivo": "marcado pelo operador"}})


def _acoes(p, fase):
    return p["fases"][fase]


def test_ordem_global_e_numeracao(p):
    seqs = [a["seq"] for f in ("criar", "renomear", "reparentar", "mover", "arquivar", "verificar", "excluir")
            for a in p["fases"][f]]
    assert seqs == list(range(1, len(seqs) + 1)) and p["modo"] == "dry-run"


def test_criar_renomear_reparentar(p):
    criar = _acoes(p, "criar")
    assert [(a["nome"], a["ref"], a["pai"]) for a in criar] == [("Cabos e Fios", "novo:cabos", 10)]
    assert criar[0]["endpoint"] == "POST /categories" and "422" in criar[0]["tratamento_422"]
    assert [(a["categoria"], a["de"], a["nome"]) for a in _acoes(p, "renomear")] == [(11, "Reles", "Relés")]
    assert [(a["categoria"], a["de"], a["para"]) for a in _acoes(p, "reparentar")] == [(21, 20, 10)]


def test_mover_respeita_folha_unica_decisao_humana_e_ignora_corretos(p):
    mov = {a["produto"]: a for a in _acoes(p, "mover")}
    assert set(mov) == {1, 3}
    assert mov[1]["de"] == [10, 11] and mov[1]["para"] == [11] and mov[1]["payload"] == {"categories": [11]}
    assert mov[3]["para"] == ["novo:cabos"] and mov[3]["origem"] == "humano" and mov[3]["confianca"] == 1.0
    assert p["resumo"]["produtos_sem_alteracao"] == 2  # 2 (contator) e 5 (disjuntor)
    assert [x["produto"] for x in p["nao_planejados"]] == [6]


def test_arquivar_usa_visibility_hidden_com_fallback_e_rollback(p):
    arq = {a["produto"]: a for a in _acoes(p, "arquivar")}
    a4 = arq[4]
    assert a4["acao"] == "arquivar_produto" and a4["endpoint"] == "PUT /products/4"
    assert a4["payload"] == {"visibility": "hidden", "categories": []}  # sai de "Acessórios" (será excluída)
    assert "visibility" not in a4["payload_fallback"] and a4["payload_fallback"]["published"] is False
    assert "published" not in a4["payload"]  # API recusa os dois juntos (422)
    assert a4["estado_anterior"] == {"published": True, "categories": [30]}
    assert a4["fora_do_ramo"] and not a4["conferir"]
    assert arq[7]["conferir"] is True and arq[7]["payload"] == {"visibility": "hidden"}  # marca Finder
    assert p["resumo"]["arquivar_conferir"] == 1 and p["resumo"]["arquivar_fora_do_ramo"] == 1


def test_excluir_com_aviso_seo_e_bloqueio_quando_sobram_produtos(p):
    exc = {a["categoria"]: a for a in _acoes(p, "excluir")}
    assert set(exc) == {30, 40}
    assert "/acessorios/" in exc[30]["aviso_seo"] and exc[30]["endpoint"] == "DELETE /categories/30"
    bloq = {a["categoria"]: a for a in p["exclusoes_bloqueadas"]}
    assert set(bloq) == {20} and "1 produto" in bloq[20]["motivo_bloqueio"]  # produto 6 em revisão
    verificar = [a["categoria"] for a in _acoes(p, "verificar")]
    assert set(verificar) == {20, 30, 40}
    assert max(a["seq"] for a in _acoes(p, "verificar")) < min(a["seq"] for a in _acoes(p, "excluir"))


def test_mesclagem_resumida(p):
    m = p["mesclagens"][0]
    assert m["origem"] == 20 and m["sobrevivente"] == 10 and m["excluir_origem"] is False


def test_executar_dry_run_exige_classificacao_da_versao_aprovada(tmp_path, monkeypatch):
    monkeypatch.setattr(modelo, "TAXONOMIA", tmp_path)
    monkeypatch.setattr(cl, "TAXONOMIA", tmp_path)
    (tmp_path / "taxonomia.json").write_text(json.dumps({**TAX, "status": modelo.STATUS_APROVADA}), encoding="utf-8")
    (tmp_path / "classificacao-1.json").write_text(json.dumps({**CLASSIF, "taxonomia_versao": 4}), encoding="utf-8")
    with pytest.raises(SystemExit, match="Rode `taxonomia classificar`"):
        plano.executar_plano_dry_run(log=lambda m: None)

    from wago.taxonomia import inventario

    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps(SNAP), encoding="utf-8")
    monkeypatch.setattr(inventario, "ultimo_snapshot", lambda: snap)
    (tmp_path / "classificacao-1.json").write_text(json.dumps({**CLASSIF, "taxonomia_versao": 5}), encoding="utf-8")
    saida = plano.executar_plano_dry_run(log=lambda m: None)
    gerado = json.loads(saida["json"].read_text(encoding="utf-8"))
    assert gerado["modo"] == "dry-run" and gerado["resumo"]["criar"] == 1
    assert "DRY-RUN" in saida["md"].read_text(encoding="utf-8")
