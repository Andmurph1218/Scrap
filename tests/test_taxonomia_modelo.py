from collections import Counter

import pytest

from wago.taxonomia import modelo


def _cat(cid, nome, pai=None):
    return {"id": cid, "nome": nome, "handle": nome.lower(), "parent_id": pai, "subcategorias": []}


def _prod(pid, *cats):
    return {"id": pid, "nome": f"P{pid}", "skus": [], "categorias": list(cats), "descricao_html": ""}


SNAPSHOT = {
    "gerado_em": "2026-11-04T08:00:00",
    "categorias": [
        _cat(1, "Automação"),                 # raiz com netos: a cadeia é preservada
        _cat(2, "Comutação e Potência", 1),
        _cat(3, "Contatores", 2),
        _cat(4, "Relés", 2),
        _cat(5, "Iluminação"),                # raiz normal
        _cat(6, "Lâmpadas", 5),
        _cat(7, "Reles"),                     # duplicada de "Relés" (menos produtos)
        _cat(8, "Vazia"),                     # sem produtos → removida
        _cat(9, "Página inicial"),            # nome inválido
        _cat(10, "Contator", 5),              # duplicada de Contatores (mais produtos) sob outra raiz
    ],
    "produtos": [
        _prod(100, 3), _prod(101, 4), _prod(102, 4), _prod(103, 7),
        _prod(104, 6), _prod(105, 10), _prod(106, 10), _prod(107, 10),
        _prod(108, 1), _prod(109, 9),
    ],
}


@pytest.fixture
def pasta(tmp_path, monkeypatch):
    monkeypatch.setattr(modelo, "TAXONOMIA", tmp_path)
    return tmp_path


def _por_nome(tax):
    return {n["nome"]: n for n in tax["nos"]}


def test_proposta_preserva_niveis_mescla_e_remove_vazias():
    tax = modelo.gerar_proposta(SNAPSHOT, Counter({"Bornes montados em trilho": 5, "relés": 2}))
    assert modelo.validar_taxonomia(tax) == []
    nos = _por_nome(tax)

    assert nos["Automação"]["pai"] == nos["Automação e Controle"]["id_alvo"]
    assert nos["Comutação e Potência"]["pai"] == nos["Automação"]["id_alvo"]
    assert nos["Relés"]["pai"] == nos["Comutação e Potência"]["id_alvo"]
    # Relés (2 prod) sobrevive a Reles (1 prod)
    assert nos["Relés"]["nuvemshop_ids_mesclados"] == [7]
    assert "Reles" in nos["Relés"]["sinonimos"]
    # Contator (3 prod, sob Iluminação) sobrevive a Contatores (1 prod)
    assert "Contatores" not in nos and nos["Contator"]["nuvemshop_ids_mesclados"] == [3]
    assert "Vazia" not in nos and "Página inicial" not in nos
    # Wago: casa com existente e cria nova raiz
    assert "wago" in nos["Relés"]["origem"] and nos["Relés"]["produtos_wago"] == 2
    assert nos["Bornes montados em trilho"]["origem"] == ["wago"]
    assert nos["Bornes montados em trilho"]["pai"] == nos["Materiais Elétricos"]["id_alvo"]
    assert {n["nome"] for n in tax["nos"] if not n["pai"]} == set(modelo.MACRO_RAIZES)

    mapa = tax["mapa_categorias_loja"]
    assert mapa["1"]["id_alvo"] == nos["Automação"]["id_alvo"]
    assert mapa["7"]["id_alvo"] == nos["Relés"]["id_alvo"]
    assert mapa["8"]["motivo"] == "vazia"
    assert mapa["9"]["motivo"] == "nome inválido"
    assert nos["Iluminação"]["produtos_estimados"] == 5  # 1 lâmpada + 3 contator + 1 contatores mesclado
    assert nos["Contator"]["produtos_estimados"] == 4


def test_mesclagem_prefere_grafia_acentuada():
    # "Reles" (id 7) passa a ter mais produtos e sobrevive, mas o nome proposto é "Relés"
    snap = {**SNAPSHOT, "produtos": SNAPSHOT["produtos"] + [_prod(110, 7), _prod(111, 7)]}
    no = _por_nome(modelo.gerar_proposta(snap))["Relés"]
    assert no["nuvemshop_id"] == 7 and no["nome_atual_loja"] == "Reles"
    assert no["nuvemshop_ids_mesclados"] == [4] and "Reles" in no["sinonimos"]


def test_validacao_rejeita_pai_inexistente_e_nome_repetido():
    tax = {"nos": [
        modelo._no("a", "A", None), modelo._no("b", "B", "a"), modelo._no("c", "C", "b"),
        modelo._no("d", "D", "x"), modelo._no("e", "B", "a"),
    ]}
    erros = " | ".join(modelo.validar_taxonomia(tax))
    assert "excede" not in erros  # três níveis cabem no teto de 4
    assert "pai inexistente" in erros
    assert "nome repetido" in erros


def _id(tax, nome):
    return next(n["id_alvo"] for n in tax["nos"] if n["nome"] == nome)


def test_nivel_4_passa_nivel_5_falha_e_ciclo_e_detectado():
    tax = {"versao": 1, "nos": [modelo._no("a", "Materiais", None)], "mesclagens": [],
           "mapa_categorias_loja": {}}
    for nome, pai in (("Instalação", "Materiais"), ("Bornes", "Instalação"), ("Passagem", "Bornes")):
        tax = modelo.editar(tax, "adicionar", nome=nome, pai=_id(tax, pai))
    assert modelo.validar_taxonomia(tax) == []
    assert _id(tax, "Passagem")  # folha no nível 4
    with pytest.raises(ValueError, match="excede 4"):
        modelo.editar(tax, "adicionar", nome="Extra", pai=_id(tax, "Passagem"))
    with pytest.raises(ValueError, match="ciclo"):
        modelo.editar(tax, "mover", id_alvo=_id(tax, "Materiais"), pai=_id(tax, "Passagem"))


def test_edicoes_do_operador():
    tax = modelo.gerar_proposta(SNAPSHOT)
    nos = _por_nome(tax)
    rel, ilu, com = nos["Relés"]["id_alvo"], nos["Iluminação"]["id_alvo"], nos["Comutação e Potência"]["id_alvo"]
    lamp = nos["Lâmpadas"]["id_alvo"]

    tax = modelo.editar(tax, "renomear", id_alvo=rel, nome="Relés e Temporizadores")
    assert "Relés" in _por_nome(tax)["Relés e Temporizadores"]["sinonimos"]

    tax = modelo.editar(tax, "adicionar", nome="Sensores", pai=None)
    tax = modelo.editar(tax, "adicionar", nome="X", pai=lamp)  # filha de folha, dentro do teto de 4
    assert _por_nome(tax)["X"]["pai"] == lamp
    with pytest.raises(ValueError):
        modelo.editar(tax, "excluir", id_alvo=com)  # tem subcategorias

    tax = modelo.editar(tax, "mesclar", origem=com, destino=ilu)
    nos = _por_nome(tax)
    assert "Comutação e Potência" not in nos
    assert nos["Relés e Temporizadores"]["pai"] == ilu
    assert 2 in nos["Iluminação"]["nuvemshop_ids_mesclados"]
    assert tax["mapa_categorias_loja"]["2"]["id_alvo"] == ilu
    assert modelo.validar_taxonomia(tax) == []


def test_gate_de_aprovacao_bloqueia_fase3(pasta):
    with pytest.raises(SystemExit, match="aprove a taxonomia"):
        modelo.exigir_taxonomia_aprovada()

    modelo.salvar_proposta(modelo.gerar_proposta(SNAPSHOT))
    assert (pasta / "taxonomia.proposta.md").exists()
    st = modelo.status_taxonomia()
    assert st["proposta_pendente"] and not st["liberado_para_classificacao"]
    with pytest.raises(SystemExit):
        modelo.exigir_taxonomia_aprovada()

    modelo.aprovar_taxonomia("operador")
    tax = modelo.exigir_taxonomia_aprovada()
    assert tax["versao"] == 1 and tax["aprovado_por"] == "operador"
    assert not (pasta / "taxonomia.proposta.json").exists()

    # editar a aprovada volta a bloquear até nova aprovação
    proposta = modelo.iniciar_edicao()
    modelo.salvar_proposta(modelo.editar(proposta, "adicionar", nome="Nova"))
    with pytest.raises(SystemExit, match="edições pendentes"):
        modelo.exigir_taxonomia_aprovada()

    modelo.aprovar_taxonomia("operador")
    assert modelo.exigir_taxonomia_aprovada()["versao"] == 2
    assert (pasta / "historico" / "taxonomia-v1.json").exists()


def test_v3_sensores_sob_automacao_e_outros_vai_para_fixacao():
    snap = {
        "gerado_em": "t",
        "categorias": [
            _cat(1, "Sensores"), _cat(2, "Sensor Indutivo", 1),
            _cat(3, "Outros"), _cat(4, "Parafusos", 3),
            _cat(5, "Iluminação"),
        ],
        "produtos": [_prod(1, 2), _prod(2, 4), _prod(3, 3)],
    }
    tax = modelo.gerar_proposta(snap)
    assert modelo.validar_taxonomia(tax) == []
    nos = _por_nome(tax)
    assert nos["Sensores"]["pai"] == nos["Automação e Controle"]["id_alvo"]
    assert nos["Sensor Indutivo"]["pai"] == nos["Sensores"]["id_alvo"]
    assert "Outros" not in nos
    assert nos["Parafusos"]["pai"] == nos["Fixação e Utilidades"]["id_alvo"]
    assert {n["nome"] for n in tax["nos"] if not n["pai"]} == set(modelo.MACRO_RAIZES)
    assert tax["mapa_categorias_loja"]["3"]["id_alvo"] == nos["Fixação e Utilidades"]["id_alvo"]


def test_ajustes_v3_move_tiristores_unifica_dps_e_traduz():
    nos = [
        modelo._no("me", "Materiais Elétricos", None),
        modelo._no("fer", "Ferramentas", None),
        modelo._no("tr", "Transformadores", "fer"),
        modelo._no("ti", "Tiristores", "fer"),
        modelo._no("dps", "Proteção contra Surtos (DPS)", "me"),
        modelo._no("sob", "Proteção contra sobretensão", "me"),
        modelo._no("pt", "Painel touch", "me"),
        modelo._no("ed", "Edge Devices", "me"),
        modelo._no("be", "Building Ecosystem", "me"),
        modelo._no("seg", "Chaves de Segurança", "me"),
    ]
    tax = {"versao": 1, "nos": nos, "mesclagens": [], "mapa_categorias_loja": {}, "status": "pendente"}
    tax = modelo.aplicar_ajustes_v3(tax)
    por = _por_nome(tax)
    assert modelo.validar_taxonomia(tax) == []
    assert por["Transformadores"]["pai"] == por["Materiais Elétricos"]["id_alvo"]
    assert por["Semicondutores"]["pai"] == por["Materiais Elétricos"]["id_alvo"]
    assert por["Tiristores"]["pai"] == por["Semicondutores"]["id_alvo"]
    assert "Proteção contra sobretensão" not in por
    assert "Proteção contra sobretensão" in por["Proteção contra Surtos (DPS)"]["sinonimos"]
    assert "Painéis Touch" in por and "Dispositivos de Borda" in por and "Ecossistema Predial" in por
    assert por["Chaves de Segurança"]["pai"] == por["Materiais Elétricos"]["id_alvo"]
    assert "Escolar" in tax["fora_do_ramo"] and "Lixeira" in tax["fora_do_ramo"]


def test_aprovacao_recusa_taxonomia_invalida(pasta):
    tax = modelo.gerar_proposta(SNAPSHOT)
    tax["nos"].append(modelo._no("z", "Z", "inexistente"))
    modelo.salvar_proposta(tax)
    with pytest.raises(SystemExit, match="inválida"):
        modelo.aprovar_taxonomia("operador")
    assert not (pasta / "taxonomia.json").exists()
