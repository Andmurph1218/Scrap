"""Taxonomia v2: edição em lote, divisão de categoria, sinônimos, fora do ramo e simulação."""

import pytest

from wago.taxonomia import classificador as cl
from wago.taxonomia import modelo


def _tax():
    nos = [
        modelo._no("iluminacao", "Iluminação", None),
        modelo._no("reator-eletronico", "Reator Eletrônico", "iluminacao"),
        modelo._no("lampadas", "Lâmpadas", "iluminacao"),
        modelo._no("protecao", "Proteção contra sobretensão", None),
        modelo._no("solar", "Solar", None),
        modelo._no("dps", "DPS - Proteção Contra Surto", "solar"),
        modelo._no("inversores", "Inversores Fotovoltaicos", "solar"),
    ]
    for n, cid in zip(nos, [1, 2, 3, 4, 5, 6, 7]):
        n["nuvemshop_id"] = cid
    return {"versao": 1, "nos": nos, "mesclagens": [],
            "mapa_categorias_loja": {str(i): {"id_alvo": n["id_alvo"], "nome": n["nome"]} for i, n in
                                     zip(range(1, 8), nos)}}


EDICOES = [
    {"acao": "mesclar", "origem": "dps", "destino": "protecao", "motivo": "sobreposição DPS"},
    {"acao": "renomear", "id_alvo": "protecao", "nome": "Proteção contra Surtos (DPS)"},
    {"acao": "sinonimos", "id_alvo": "protecao", "sinonimos": ["DPS", "Protetor de Surto"]},
    {"acao": "adicionar", "nome": "Reator Convencional", "pai": "iluminacao", "divisao_de": "reator-eletronico",
     "sinonimos": ["Reator Eletromagnético", "Reator Partida Rápida"]},
    {"acao": "adicionar", "nome": "Canaletas e Acessórios", "pai": None, "origem_no": "ia", "sinonimos": ["Mata Junta"]},
    {"acao": "fora_do_ramo", "termos": ["Utensílios de Cozinha", "Papel Sulfite"]},
]


def _por_nome(tax):
    return {n["nome"]: n for n in tax["nos"]}


def test_editar_lote_v2_dps_unificado_e_reator_dividido():
    tax = modelo.editar_lote(_tax(), EDICOES)
    nos = _por_nome(tax)
    assert modelo.validar_taxonomia(tax) == []
    dps = nos["Proteção contra Surtos (DPS)"]
    assert dps["nuvemshop_id"] == 4 and dps["nuvemshop_ids_mesclados"] == [6]
    assert dps["nome_atual_loja"] == "Proteção contra sobretensão"
    assert {"DPS", "Protetor de Surto", "DPS - Proteção Contra Surto"} <= set(dps["sinonimos"])
    assert "DPS - Proteção Contra Surto" not in nos
    assert tax["mapa_categorias_loja"]["6"]["id_alvo"] == "protecao"
    conv = nos["Reator Convencional"]
    assert conv["divisao_de"] == "reator-eletronico" and conv["nuvemshop_id"] is None
    assert nos["Reator Eletrônico"]["reavaliar"] is True
    assert nos["Canaletas e Acessórios"]["origem"] == ["ia"]  # "Acessórios" no nome composto é permitido
    assert tax["fora_do_ramo"] == ["Papel Sulfite", "Utensílios de Cozinha"]


def test_editar_lote_informa_edicao_com_erro_e_nao_aplica_parcial():
    with pytest.raises(ValueError, match="edição #2"):
        modelo.editar_lote(_tax(), [EDICOES[0], {"acao": "adicionar", "nome": "Geral", "pai": None}])
    with pytest.raises(ValueError, match="categoria final"):
        modelo.editar(_tax(), "adicionar", nome="X", pai="iluminacao", divisao_de="iluminacao")


def test_classificador_reavalia_categoria_dividida():
    tax = modelo.editar_lote(_tax(), EDICOES)
    idx = cl.IndiceTaxonomia.criar(tax)

    def prod(pid, nome):
        return {"id": pid, "nome": nome, "categorias": [2], "skus": [], "descricao_html": ""}

    conv = cl.classificar_produto(prod(1, "Reator Partida Rápida 2x40W Philips"), tax, idx)
    assert conv["categoria_id"] == "reator-convencional" and conv["status"] == cl.STATUS_AUTO
    assert conv["origem"] == "loja+regras"
    elet = cl.classificar_produto(prod(2, "Reator Eletrônico 2x32W Bivolt"), tax, idx)
    assert elet["categoria_id"] == "reator-eletronico" and elet["origem"] == "loja"
    neutro = cl.classificar_produto(prod(3, "Reator 1x40W"), tax, idx)
    assert neutro["categoria_id"] == "reator-eletronico"  # sem evidência: mantém a categoria atual


def test_dps_de_qualquer_arvore_cai_na_categoria_unificada():
    tax = modelo.editar_lote(_tax(), EDICOES)
    idx = cl.IndiceTaxonomia.criar(tax)
    r = cl.classificar_produto({"id": 9, "nome": "DPS Classe II 20kA 275V", "categorias": [6], "skus": [],
                                "descricao_html": ""}, tax, idx)
    assert r["categoria_id"] == "protecao" and r["status"] == cl.STATUS_AUTO


def test_fora_do_ramo_por_nome_e_por_proposta_da_ia():
    tax = modelo.editar_lote(_tax(), EDICOES)
    r = cl.marcar_fora_do_ramo({"nome": "Papel Sulfite A4 500 folhas"}, tax)
    assert r["fora_do_ramo"] and r["fora_do_ramo_motivo"] == "papel sulfite"
    r = cl.marcar_fora_do_ramo({"nome": "Espremedor X", "nova_categoria": {"nome": "Utensílios de Cozinha"}}, tax)
    assert r["fora_do_ramo"]
    assert "fora_do_ramo" not in cl.marcar_fora_do_ramo({"nome": "Reator 40W"}, tax)


def test_simulacao_conta_produtos_por_no_e_tira_alerta_de_nova(monkeypatch, tmp_path):
    monkeypatch.setattr(cl, "TAXONOMIA", tmp_path)
    monkeypatch.setattr(cl, "categorias_wago_por_sku", lambda: {})
    tax = modelo.editar_lote(_tax(), EDICOES)
    assert not any("poucos produtos" in a for a in _por_nome(tax)["Reator Convencional"]["alertas"])
    snap = {"produtos": [
        {"id": 1, "nome": "Reator Partida Rápida 2x40W", "categorias": [2], "skus": [], "descricao_html": ""},
        {"id": 2, "nome": "Reator Eletrônico 2x32W", "categorias": [2], "skus": [], "descricao_html": ""},
        {"id": 3, "nome": "Mata Junta Canaleta 20x10", "categorias": [], "skus": [], "descricao_html": ""},
    ]}
    sim = cl.simular_proposta(tax, snap, log=lambda m: None)
    nos = _por_nome(sim)
    assert nos["Reator Convencional"]["produtos_simulados"] == 1
    assert nos["Canaletas e Acessórios"]["produtos_simulados"] == 1
    assert nos["Iluminação"]["produtos_simulados"] == 2
    assert sim["simulacao"]["categorizados_auto"] == 3
