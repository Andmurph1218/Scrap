"""Regras do cliente (edge cases reais): tradução Wago, blacklist, 40 caracteres, consolidação."""

from collections import Counter

import pytest

from wago.taxonomia import classificador, inventario, modelo, plano


def _cat(cid, nome, pai=None):
    return {"id": cid, "nome": nome, "handle": f"h{cid}", "parent_id": pai, "subcategorias": []}


def _prod(pid, *cats):
    return {"id": pid, "nome": f"P{pid}", "skus": [], "categorias": list(cats), "descricao_html": ""}


ME, CP = 100, 200
SNAPSHOT = {
    "gerado_em": "2026-11-04T08:00:00",
    "categorias": [
        _cat(ME, "Materiais Elétricos"),
        _cat(101, "Relés", ME), _cat(102, "Disjuntor", ME), _cat(103, "Bobina", ME),
        _cat(CP, "Comutação e Potência"),
        _cat(201, "Contatores", CP), _cat(202, "Relés", CP),  # Relés repetido nas duas árvores
        _cat(300, "Interface Modules and System Wiring"),
        _cat(301, "Bornes para placas de circuito impresso e conectores plugáveis"),
        _cat(302, "Outros"),
        _cat(303, "Pneumática"),
    ],
    "produtos": [
        _prod(1, 101), _prod(2, 101), _prod(3, 102), _prod(4, 102), _prod(5, 103), _prod(6, 103),
        _prod(7, 201), _prod(8, 201), _prod(9, 201), _prod(10, 202),
        _prod(11, CP), _prod(12, CP),  # produtos direto na raiz de origem
        _prod(13, 300), _prod(14, 300), _prod(15, 300),
        _prod(16, 301), _prod(17, 301), _prod(18, 301),
        _prod(19, 302), _prod(20, 302), _prod(21, 302),
        _prod(22, 303),  # categoria com 1 produto → consolidar
    ],
}


def _por_nome(tax):
    return {n["nome"]: n for n in tax["nos"]}


# 1) Dicionário de tradução Wago -------------------------------------------------
@pytest.mark.parametrize("en,pt", [
    ("Interface Modules and System Wiring", "Módulos de Interface e Cabeamento"),
    ("Energy Measurement Technology", "Medição de Energia"),
    ("Component Modules", "Módulos de Componentes"),
])
def test_traducao_wago(en, pt):
    assert modelo.ajustar_nome(en) == pt
    assert modelo.ajustar_nome(en.lower()) == pt


def test_proposta_traduz_categoria_da_loja_e_da_wago():
    tax = modelo.gerar_proposta(SNAPSHOT, Counter({"Interface Modules and System Wiring": 4,
                                                   "Energy Measurement Technology": 2}))
    nos = _por_nome(tax)
    no = nos["Módulos de Interface e Cabeamento"]
    assert no["nuvemshop_id"] == 300 and no["nome_atual_loja"] == "Interface Modules and System Wiring"
    assert "Interface Modules and System Wiring" in no["sinonimos"] and "wago" in no["origem"]
    assert nos["Medição de Energia"]["origem"] == ["wago"]  # nova raiz já traduzida
    assert "Interface Modules and System Wiring" not in nos


def test_prompt_instrui_traducao_blacklist_e_limite():
    prompt = classificador.montar_prompt("- reles: Relés", "- 1: Relé 24V")
    assert "TRADUZA automaticamente" in prompt
    assert '"Interface Modules and System Wiring" → "Módulos de Interface e Cabeamento"' in prompt
    assert "Acessórios, Diversos, Geral, Outros" in prompt
    assert "no máximo 40 caracteres" in prompt and "Bornes para PCI e Conectores" in prompt


# 2) Blacklist de categorias genéricas -------------------------------------------
TAX_SIMPLES = {"nos": [
    modelo._no("reles", "Relés", None),
    modelo._no("outros", "Outros", None),
    modelo._no("acessorios", "Acessórios", None),
    modelo._no("diversos", "Diversos", None),
    modelo._no("geral", "Geral", None),
    modelo._no("eletrica", "Elétrica", None),
    modelo._no("cabos", "Cabos", "eletrica"),
]}


@pytest.mark.parametrize("cat", ["outros", "acessorios", "diversos", "geral"])
def test_ia_retornando_categoria_generica_vai_para_revisao(cat):
    r = classificador.aplicar_regras(
        {"produto_id": 1, "categoria_id": cat, "confianca": 0.99, "justificativa": "x"}, TAX_SIMPLES
    )
    assert r["status"] == classificador.STATUS_REVISAO
    assert r["confianca"] == 0.0 and r["categoria_id"] is None
    assert "genérica bloqueada" in r["motivos_revisao"][0]


def test_regras_de_status_auto_limiar_folha_e_inexistente():
    base = {"produto_id": 1, "justificativa": "x"}
    assert classificador.aplicar_regras({**base, "categoria_id": "reles", "confianca": 0.9}, TAX_SIMPLES)["status"] \
        == classificador.STATUS_AUTO
    baixa = classificador.aplicar_regras({**base, "categoria_id": "reles", "confianca": 0.84}, TAX_SIMPLES)
    assert baixa["status"] == classificador.STATUS_REVISAO and baixa["confianca"] == 0.84
    pai = classificador.aplicar_regras({**base, "categoria_id": "eletrica", "confianca": 0.95}, TAX_SIMPLES)
    assert pai["status"] == classificador.STATUS_REVISAO and pai["confianca"] == 0.0
    fake = classificador.aplicar_regras({**base, "categoria_id": "inventada", "confianca": 0.95}, TAX_SIMPLES)
    assert fake["status"] == classificador.STATUS_REVISAO and fake["categoria_id"] is None


def test_nova_categoria_generica_recusada():
    r = classificador.aplicar_regras(
        {"produto_id": 1, "categoria_id": None, "confianca": 0.9, "nova_categoria": {"nome": "Diversos"}},
        TAX_SIMPLES,
    )
    assert r["nova_categoria"] is None and r["status"] == classificador.STATUS_REVISAO


def test_validador_bloqueia_generica_folha_mas_aceita_como_pai():
    erros = " | ".join(modelo.validar_taxonomia(TAX_SIMPLES))
    for nome in ("Outros", "Acessórios", "Diversos", "Geral"):
        assert f"{nome}: categoria genérica" in erros
    tax = {"nos": [modelo._no("outros", "Outros", None), modelo._no("parafusos", "Parafusos", "outros")]}
    assert modelo.validar_taxonomia(tax) == []


def test_proposta_remove_genericas_finais_e_marca_produtos_para_reclassificar():
    snap = {**SNAPSHOT, "categorias": SNAPSHOT["categorias"] + [_cat(304, "Acessórios")],
            "produtos": SNAPSHOT["produtos"] + [_prod(30, 304), _prod(31, 304), _prod(32, 304)]}
    tax = modelo.gerar_proposta(snap, Counter({"Acessórios": 12}))
    nomes = _por_nome(tax)
    assert "Acessórios" not in nomes and "Outros" not in nomes
    info_ac = tax["mapa_categorias_loja"]["304"]
    assert info_ac["id_alvo"] is None and info_ac["reclassificar"] is True and "Fase 3" in info_ac["motivo"]
    info_outros = tax["mapa_categorias_loja"]["302"]
    assert info_outros["id_alvo"] == nomes["Fixação e Utilidades"]["id_alvo"]
    assert "Fixação e Utilidades" in info_outros["motivo"]
    assert {g["nome"] for g in tax["genericas_removidas"]} == {"Acessórios", "Outros"}
    assert tax["resumo"]["produtos_para_reclassificar_genericas"] == 6
    assert modelo.validar_taxonomia(tax) == []  # gate liberado
    # se a IA ainda tentar "acessorios", a regra joga para revisão humana
    r = classificador.aplicar_regras(
        {"produto_id": 30, "categoria_id": "acessorios", "confianca": 0.99}, tax
    )
    assert r["status"] == classificador.STATUS_REVISAO and r["confianca"] == 0.0


def test_generica_com_subcategorias_permanece_como_pai():
    snap = {**SNAPSHOT, "categorias": SNAPSHOT["categorias"] + [_cat(305, "Parafusos", 302)],
            "produtos": SNAPSHOT["produtos"] + [_prod(40, 305), _prod(41, 305), _prod(42, 305)]}
    nos = _por_nome(modelo.gerar_proposta(snap))
    assert "Outros" not in nos
    assert nos["Parafusos"]["pai"] == nos["Fixação e Utilidades"]["id_alvo"]


def test_renomear_outros_gera_acao_no_plano_preservando_id_e_subcategorias():
    snap = {**SNAPSHOT, "categorias": SNAPSHOT["categorias"] + [_cat(305, "Parafusos", 302), _cat(306, "Cadeados", 302)],
            "produtos": SNAPSHOT["produtos"] + [_prod(40, 305), _prod(41, 305), _prod(42, 306)]}
    # A geração v3 já elimina Outros. O renomear continua preservando id e filhas
    # quando o operador faz a troca à mão, antes dessa realocação.
    tax = {"nos": [
        modelo._no("outros", "Outros", None),
        modelo._no("parafusos", "Parafusos", "outros"),
        modelo._no("cadeados", "Cadeados", "outros"),
    ], "mesclagens": [], "mapa_categorias_loja": {}}
    tax["nos"][0]["nuvemshop_id"] = 302
    tax["nos"][1]["nuvemshop_id"] = 305
    tax["nos"][2]["nuvemshop_id"] = 306
    outros = tax["nos"][0]
    tax = modelo.editar(tax, "renomear", id_alvo=outros["id_alvo"], nome="Utilidades e Manutenção")
    nos = _por_nome(tax)
    novo = nos["Utilidades e Manutenção"]
    assert novo["id_alvo"] == outros["id_alvo"] and novo["nuvemshop_id"] == 302
    assert novo["nome_atual_loja"] == "Outros" and "Outros" in novo["sinonimos"]
    assert nos["Parafusos"]["pai"] == novo["id_alvo"] and nos["Cadeados"]["pai"] == novo["id_alvo"]
    assert modelo.validar_taxonomia(tax) == []

    acoes = plano.acoes_renomear(tax, snap)
    ren = next(a for a in acoes if a["categoria"] == 302)
    assert ren == {**ren, "acao": "renomear", "de": "Outros", "nome": "Utilidades e Manutenção",
                   "subcategorias_preservadas": [305, 306]}
    # nenhuma subcategoria é reparentada e nada é excluído por causa da renomeação
    grupos = plano.grupos_mesclagem(tax, snap)
    assert not any(a.get("categoria") in (302, 305, 306) and a["acao"] in ("reparentar", "excluir")
                   for g in grupos for a in g["acoes"])
    # sobreviventes de mesclagem já carregam o próprio renomear dentro do grupo
    assert all(a["categoria"] != ME for a in plano.acoes_renomear(tax, snap, ignorar={ME}))

    # renomear de volta para o nome da loja remove a ação
    tax = modelo.editar(tax, "renomear", id_alvo=outros["id_alvo"], nome="Outros Itens")
    tax = modelo.editar(tax, "renomear", id_alvo=outros["id_alvo"], nome="Outros")
    assert "nome_atual_loja" not in _por_nome(tax)["Outros"]


# 3) Limite de 40 caracteres -----------------------------------------------------
def test_validador_bloqueia_nome_acima_de_40():
    tax = {"nos": [modelo._no("x", "Bornes para placas de circuito impresso e conectores plugáveis", None)]}
    assert any("máx. 40" in e for e in modelo.validar_taxonomia(tax))
    tax = modelo.editar({"nos": [modelo._no("a", "A", None)]}, "adicionar", nome="Curto")
    with pytest.raises(ValueError, match="máx. 40"):
        modelo.editar(tax, "renomear", id_alvo="a", nome="X" * 41)


def test_nome_longo_e_resumido_na_proposta_e_na_sugestao_da_ia():
    assert modelo.ajustar_nome("Bornes para placas de circuito impresso e conectores plugáveis") \
        == "Bornes para PCI e Conectores"
    assert "Bornes para PCI e Conectores" in _por_nome(modelo.gerar_proposta(SNAPSHOT))

    r = classificador.aplicar_regras({
        "produto_id": 1, "categoria_id": None, "confianca": 0.9,
        "nova_categoria": {"nome": "Bornes para placas de circuito impresso e conectores plugáveis"},
    }, TAX_SIMPLES)
    assert r["nova_categoria"]["nome"] == "Bornes para PCI e Conectores"
    assert r["nova_categoria"]["nome_original"].startswith("Bornes para placas")

    r = classificador.aplicar_regras({
        "produto_id": 1, "categoria_id": None, "confianca": 0.9,
        "nova_categoria": {"nome": "Categoria inventada com um nome extremamente comprido demais"},
    }, TAX_SIMPLES)
    assert r["nova_categoria"] is None and "máx. 40" in r["motivos_revisao"][0]


# 4) Consolidação ----------------------------------------------------------------
def test_diagnostico_sugere_consolidar_categorias_com_ate_2_produtos():
    diag = inventario.diagnosticar(SNAPSHOT)
    sug = {c["id"]: c for c in diag["consolidacao_sugerida"]}
    assert 303 in sug and "mesclar" in sug[303]["sugestao"]  # Pneumática: 1 produto
    assert 202 in sug and "Comutação e Potência" in sug[202]["sugestao"]  # Relés (CP): 1 produto
    assert 101 in sug  # 2 produtos
    assert 201 not in sug  # 3 produtos
    assert diag["totais"]["categorias_consolidacao_sugerida"] == len(sug)
    assert "Consolidação sugerida" in inventario.relatorio_markdown(diag)


def test_proposta_alerta_poucos_produtos():
    no = _por_nome(modelo.gerar_proposta(SNAPSHOT))["Pneumática"]
    assert any("poucos produtos" in a for a in no["alertas"])


def test_arvores_duplicadas_materiais_eletricos_sobrevive():
    tax = modelo.gerar_proposta(SNAPSHOT)
    nos = _por_nome(tax)
    assert "Comutação e Potência" not in nos
    me = nos["Materiais Elétricos"]
    assert CP in me["nuvemshop_ids_mesclados"]
    assert nos["Contatores"]["pai"] == me["id_alvo"]
    # "Relés" das duas árvores vira uma só subcategoria de Materiais Elétricos
    assert nos["Relés"]["pai"] == me["id_alvo"] and 202 in nos["Relés"]["nuvemshop_ids_mesclados"]
    arvore = [m for m in tax["mesclagens"] if m["tipo"] == "arvore"]
    assert arvore == [{**arvore[0], "destino_nuvemshop_id": ME, "origem_nuvemshop_id": CP,
                       "motivo": "árvores duplicadas"}]


def test_sobrevivente_e_a_arvore_com_mais_filhas_mesmo_com_menos_produtos():
    snap = {**SNAPSHOT, "produtos": SNAPSHOT["produtos"] + [_prod(900 + i, 201) for i in range(30)]}
    tax = modelo.gerar_proposta(snap)
    assert "Materiais Elétricos" in _por_nome(tax) and "Comutação e Potência" not in _por_nome(tax)


def test_plano_expande_mesclagem_de_arvore_em_acoes_atomicas():
    tax = modelo.gerar_proposta(SNAPSHOT)
    grupos = plano.grupos_mesclagem(tax, SNAPSHOT)
    g = grupos[0]
    assert g["tipo"] == "mesclagem" and g["subtipo"] == "arvore"
    assert g["sobrevivente"] == ME and g["origens"] == [CP]
    acoes = [a["acao"] for a in g["acoes"]]
    # ordem obrigatória: reparentar → mover → verificar → excluir
    assert acoes == ["reparentar", "reparentar", "mover_produto", "mover_produto", "verificar_vazia", "excluir"]
    rep = {a["categoria"]: a for a in g["acoes"] if a["acao"] == "reparentar"}
    assert rep[201]["para"] == ME and rep[202]["de"] == CP
    movs = [a for a in g["acoes"] if a["acao"] == "mover_produto"]
    assert {m["produto"] for m in movs} == {11, 12}
    assert all(m["para"] == [ME] and m["requer_classificacao"] for m in movs)  # ME tem filhas
    assert g["acoes"][-1]["aviso_seo"] is True

    # com resultado da classificação, o produto vai direto para a categoria final
    g2 = plano.grupos_mesclagem(tax, SNAPSHOT, destino_produto={11: 101})[0]
    mov11 = next(a for a in g2["acoes"] if a.get("produto") == 11)
    assert mov11["para"] == [101] and not mov11["requer_classificacao"]
