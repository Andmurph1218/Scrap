"""Fase 3 — sanitização e cascata determinística (sem IA)."""

import json

import pytest

from wago.taxonomia import classificador as cl
from wago.taxonomia import modelo
from wago.taxonomia.texto import sanitizar_texto


def test_sanitizar_remove_html_scripts_entidades_urls_e_limita():
    html = (
        "<p><strong>Bloco de Contato</strong>&nbsp;HBE-102</p><script>alert(1)</script>"
        "<style>.x{}</style><ul><li>1NF&amp;10A</li></ul> veja https://loja.com/x &#39;ok&#39; 😀"
    )
    assert sanitizar_texto(html) == "Bloco de Contato HBE-102 1NF&10A veja 'ok'"
    assert sanitizar_texto(None) == ""
    assert sanitizar_texto("&amp;nbsp;texto") == "texto"
    longo = sanitizar_texto("palavra " * 500, limite=50)
    assert len(longo) <= 50 and not longo.endswith(" ")


def _tax():
    nos = [
        modelo._no("materiais", "Materiais Elétricos", None),
        modelo._no("reles", "Relés", "materiais"),
        modelo._no("disjuntor", "Disjuntor", "materiais"),
        modelo._no("contatores", "Contatores", "materiais"),
        modelo._no("contatos-auxiliares", "Contatos Auxiliares", "materiais"),
        modelo._no("iluminacao", "Iluminação", None),
        modelo._no("lampadas", "Lâmpadas", "iluminacao"),
        modelo._no("plugues", "Plugues", "iluminacao"),
        modelo._no("prensa-cabos", "Prensa Cabos", "iluminacao"),
        modelo._no("bornes-trilho", "Bornes montados em trilho", None),
    ]
    nos[1]["sinonimos"] = ["Reles"]
    return {
        "nos": nos,
        "mapa_categorias_loja": {
            "10": {"id_alvo": "reles"}, "11": {"id_alvo": "disjuntor"},
            "20": {"id_alvo": "materiais"}, "30": {"id_alvo": None, "reclassificar": True},
            "40": {"id_alvo": "bornes-trilho"}, "50": {"id_alvo": "lampadas"},
        },
    }


def _prod(pid, nome, cats=(), skus=(), desc=""):
    return {"id": pid, "nome": nome, "categorias": list(cats), "skus": list(skus), "descricao_html": desc}


@pytest.fixture
def ctx():
    tax = _tax()
    return tax, cl.IndiceTaxonomia.criar(tax)


def test_genericas_e_pais_nao_sao_folhas(ctx):
    _, idx = ctx
    assert "materiais" not in idx.folhas and "reles" in idx.folhas


def test_precedencia_humano_wago_loja(ctx):
    tax, idx = ctx
    p = _prod(1, "Rele 24V", cats=[11], skus=["2002-1201"])
    assert cl.classificar_produto(p, tax, idx, decisoes={"1": "contatores"})["origem"] == "humano"
    r = cl.classificar_produto(p, tax, idx, wago_por_sku={"2002-1201": "Bornes montados em trilho"})
    assert r["origem"] == "wago" and r["categoria_id"] == "bornes-trilho" and r["confianca"] == 0.95
    r = cl.classificar_produto(p, tax, idx)
    assert r["origem"] == "loja" and r["categoria_id"] == "disjuntor" and r["status"] == cl.STATUS_AUTO


def test_reclassificar_ignora_categoria_atual_e_usa_descricao_do_nome(ctx):
    tax, idx = ctx
    r = cl.classificar_produto(_prod(2, "Contator Tripolar 25A Schneider", cats=[30]), tax, idx)
    assert r["categoria_id"] == "contatores" and r["origem"] == "regras" and r["status"] == cl.STATUS_AUTO


def test_produto_em_categoria_pai_vai_para_filha_por_palavra_chave(ctx):
    tax, idx = ctx
    r = cl.classificar_produto(_prod(3, "Rele Falta de Fase 480V", cats=[20]), tax, idx)
    assert r["categoria_id"] == "reles" and r["status"] == cl.STATUS_AUTO


def test_produto_em_pai_e_filha_usa_a_filha(ctx):
    tax, idx = ctx
    r = cl.classificar_produto(_prod(4, "Qualquer coisa", cats=[20, 10]), tax, idx)
    assert r["origem"] == "loja" and r["categoria_id"] == "reles"


def test_varias_folhas_atuais_desempata_por_palavra_chave(ctx):
    tax, idx = ctx
    r = cl.classificar_produto(_prod(5, "Disjuntor Bipolar 25A", cats=[10, 11]), tax, idx)
    assert r["categoria_id"] == "disjuntor" and r["status"] == cl.STATUS_AUTO


def test_sinonimo_de_token_plug_vence_prensa_cabo(ctx):
    tax, idx = ctx
    r = cl.classificar_produto(_prod(6, "Plug 2P+T Prensa Cabo Pial"), tax, idx)
    assert r["categoria_id"] == "plugues"


def test_frase_mais_especifica_vence_empate():
    tax = {"nos": [modelo._no("bobina", "Bobina", None), modelo._no("bob-cont", "Bobinas para Contatores", None),
                   modelo._no("contatores", "Contatores", None)], "mapa_categorias_loja": {}}
    idx = cl.IndiceTaxonomia.criar(tax)
    r = cl.classificar_produto(_prod(1, "Bobina 3rt1965 220v Para Contator 3rt1065 Siemens"), tax, idx)
    assert r["categoria_id"] == "bob-cont" and r["status"] == cl.STATUS_AUTO
    r = cl.classificar_produto(_prod(2, "Bobina 24V"), tax, idx)
    assert r["categoria_id"] == "bobina" and r["status"] == cl.STATUS_AUTO


def test_sem_correspondencia_vai_para_revisao(ctx):
    tax, idx = ctx
    r = cl.classificar_produto(_prod(7, "Calefator 220V 250W"), tax, idx)
    assert r["status"] == cl.STATUS_REVISAO and r["categoria_id"] is None
    ambiguo = cl.classificar_produto(_prod(8, "Painel com rele e disjuntor"), tax, idx)
    assert ambiguo["status"] == cl.STATUS_REVISAO
    assert {s["categoria_id"] for s in ambiguo["sugestoes"][:2]} == {"reles", "disjuntor"}
    # termo do tipo do produto no início do nome desempata
    r = cl.classificar_produto(_prod(10, "Contato auxiliar para contator"), tax, idx)
    assert r["categoria_id"] == "contatos-auxiliares" and r["status"] == cl.STATUS_AUTO


def test_descricao_html_sanitizada_e_usada_com_peso_menor(ctx):
    tax, idx = ctx
    r = cl.classificar_produto(
        _prod(9, "Item XPTO 123", desc="<p>Lâmpada <b>halógena</b></p> Dúvidas Frequentes relé"), tax, idx
    )
    assert r["status"] == cl.STATUS_REVISAO
    assert r["sugestoes"][0]["categoria_id"] == "lampadas"
    assert all(s["categoria_id"] != "reles" for s in r["sugestoes"])  # texto após FAQ é ignorado


def test_lote_resumo_e_relatorio(ctx):
    tax, _ = ctx
    snap = {"produtos": [
        _prod(1, "Rele 24V", cats=[10]), _prod(2, "Calefator"), _prod(3, "Disjuntor 10A", cats=[30]),
    ]}
    res = cl.classificar_lote(snap, tax)
    resumo = cl.resumir(res)
    assert resumo["total"] == 3 and resumo["categorizados_auto"] == 2 and resumo["aguardando_revisao"] == 1
    assert "Classificação (sem IA)" in cl.relatorio_classificacao(resumo, res)


def test_falta_informacao_nao_recebe_categoria(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "TAXONOMIA", tmp_path)
    tax, _ = ctx
    (tmp_path / "arquivar.json").write_text(json.dumps({
        "1": {"motivo": cl.MOTIVO_FALTA_INFORMACAO},
        "9": {"motivo": "marcado pelo operador"},
    }), encoding="utf-8")
    snap = {"produtos": [_prod(1, "Rele 24V", cats=[10]), _prod(2, "Disjuntor 10A")]}
    res = {r["produto_id"]: r for r in cl.classificar_lote(snap, tax, decisoes={"1": "reles"})}
    assert res[1]["categoria_id"] is None and res[1]["falta_informacao"] is True
    assert res[1]["justificativa"] == cl.TEXTO_REVISAO_CADASTRAL
    assert res[2]["categoria_id"] == "disjuntor"
    cl.sincronizar_arquivar([
        {"produto_id": 2, "fora_do_ramo": True, "fora_do_ramo_motivo": "espremedor"},
        {"produto_id": 1, "fora_do_ramo": True, "fora_do_ramo_motivo": "nao deve sobrescrever"},
    ])
    gravado = json.loads((tmp_path / "arquivar.json").read_text(encoding="utf-8"))
    assert gravado["1"]["motivo"] == cl.MOTIVO_FALTA_INFORMACAO
    assert gravado["9"]["motivo"] == "marcado pelo operador"
    assert gravado["2"]["motivo"] == "espremedor"


def test_executar_classificacao_exige_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(modelo, "TAXONOMIA", tmp_path)
    with pytest.raises(SystemExit, match="Fase 3 bloqueada"):
        cl.executar_classificacao(log=lambda m: None)
