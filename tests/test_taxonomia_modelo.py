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
        _cat(1, "Automação"),                 # raiz com netos → dissolvida
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


def test_proposta_achata_2_niveis_mescla_e_remove_vazias():
    tax = modelo.gerar_proposta(SNAPSHOT, Counter({"Bornes montados em trilho": 5, "relés": 2}))
    assert modelo.validar_taxonomia(tax) == []
    nos = _por_nome(tax)

    assert "Automação" not in nos  # raiz dissolvida
    assert nos["Comutação e Potência"]["pai"] is None
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

    mapa = tax["mapa_categorias_loja"]
    assert mapa["1"]["id_alvo"] is None and "dissolvida" in mapa["1"]["motivo"]
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


def test_validacao_rejeita_3_niveis_pai_inexistente_e_nome_repetido():
    tax = {"nos": [
        modelo._no("a", "A", None), modelo._no("b", "B", "a"), modelo._no("c", "C", "b"),
        modelo._no("d", "D", "x"), modelo._no("e", "B", "a"),
    ]}
    erros = " | ".join(modelo.validar_taxonomia(tax))
    assert "excede 2 níveis" in erros
    assert "pai inexistente" in erros
    assert "nome repetido" in erros


def test_edicoes_do_operador():
    tax = modelo.gerar_proposta(SNAPSHOT)
    nos = _por_nome(tax)
    rel, ilu, com = nos["Relés"]["id_alvo"], nos["Iluminação"]["id_alvo"], nos["Comutação e Potência"]["id_alvo"]

    tax = modelo.editar(tax, "renomear", id_alvo=rel, nome="Relés e Temporizadores")
    assert "Relés" in _por_nome(tax)["Relés e Temporizadores"]["sinonimos"]

    tax = modelo.editar(tax, "adicionar", nome="Sensores", pai=None)
    with pytest.raises(ValueError):
        modelo.editar(tax, "adicionar", nome="X", pai=rel)  # rel é filho → 3º nível
    with pytest.raises(ValueError):
        modelo.editar(tax, "mover", id_alvo=com, pai=ilu)  # tem filhos → 3º nível
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


def test_aprovacao_recusa_taxonomia_invalida(pasta):
    tax = modelo.gerar_proposta(SNAPSHOT)
    tax["nos"].append(modelo._no("z", "Z", "inexistente"))
    modelo.salvar_proposta(tax)
    with pytest.raises(SystemExit, match="inválida"):
        modelo.aprovar_taxonomia("operador")
    assert not (pasta / "taxonomia.json").exists()
