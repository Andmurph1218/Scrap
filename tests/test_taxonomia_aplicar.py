"""Fase 5 — aplicação e reversão contra uma loja simulada em memória."""

import copy
import json

import pytest

from wago.taxonomia import aplicar
from wago.taxonomia.nuvemshop_api import ErroNuvemshop


class LojaFake:
    """Simula os endpoints usados: categorias (handle fixo ao reparentar) e produtos (visibility)."""

    store_id = "123"

    def __init__(self):
        self.prox = 900
        self.cats = {
            10: {"id": 10, "name": {"pt": "Materiais"}, "handle": {"pt": "materiais"}, "parent": None, "subcategories": [21]},
            20: {"id": 20, "name": {"pt": "Comutação"}, "handle": {"pt": "comutacao"}, "parent": None, "subcategories": []},
            21: {"id": 21, "name": {"pt": "Contatores"}, "handle": {"pt": "contatores"}, "parent": 10, "subcategories": []},
            30: {"id": 30, "name": {"pt": "Vazia"}, "handle": {"pt": "vazia"}, "parent": 10, "subcategories": [],
                 "seo_title": "Vazia SEO"},
            40: {"id": 40, "name": {"pt": "Papelaria"}, "handle": {"pt": "papelaria"}, "parent": None, "subcategories": []},
        }
        self.cats[10]["subcategories"] = [21, 30]
        self.prods = {
            1: {"id": 1, "categories": [10, 21], "visibility": "visible", "published": True},
            2: {"id": 2, "categories": [40], "visibility": "visible", "published": True},
            3: {"id": 3, "categories": [40], "visibility": "visible", "published": True},
        }
        self.chamadas = []

    def _sync_subs(self):
        for c in self.cats.values():
            c["subcategories"] = [x["id"] for x in self.cats.values() if x["parent"] == c["id"]]

    def obter_categoria(self, cid):
        if cid not in self.cats:
            raise ErroNuvemshop(404, "Not Found")
        return copy.deepcopy(self.cats[cid])

    def listar_categorias(self):
        return copy.deepcopy(list(self.cats.values()))

    def criar_categoria(self, nome, pai=None, **extra):
        self.chamadas.append(("POST", nome))
        for c in self.cats.values():
            if c["name"]["pt"] == nome and c["parent"] == pai:
                return copy.deepcopy(c), True  # simula 422 + reutilização
        self.prox += 1
        handle = extra.get("handle") or {"pt": nome.lower().replace(" ", "-")}
        self.cats[self.prox] = {"id": self.prox, "name": {"pt": nome}, "handle": handle, "parent": pai,
                                "subcategories": [], **{k: v for k, v in extra.items() if k != "handle"}}
        self._sync_subs()
        return copy.deepcopy(self.cats[self.prox]), False

    def atualizar_categoria(self, cid, **campos):
        self.chamadas.append(("PUT cat", cid, campos))
        self.cats[cid].update(campos)
        self._sync_subs()
        return copy.deepcopy(self.cats[cid])

    def excluir_categoria(self, cid):
        self.chamadas.append(("DELETE", cid))
        self.cats.pop(cid, None)
        self._sync_subs()

    def produtos_da_categoria(self, cid):
        return [p for p in self.prods.values() if cid in p["categories"]]

    def obter_produto(self, pid):
        return self._vista(pid)

    def _vista(self, pid):
        p = copy.deepcopy(self.prods[pid])
        p["categories"] = [{"id": c} for c in p["categories"]]
        return p

    def atualizar_produto(self, pid, **campos):
        self.chamadas.append(("PUT prod", pid, campos))
        if "visibility" in campos and "published" in campos:
            raise ErroNuvemshop(422, "Cannot send both")
        p = self.prods[pid]
        if "visibility" in campos:
            p["visibility"], p["published"] = campos["visibility"], campos["visibility"] == "visible"
        if "published" in campos:
            p["published"], p["visibility"] = campos["published"], "visible" if campos["published"] else "hidden"
        if "categories" in campos:
            p["categories"] = list(campos["categories"])
        return self._vista(pid)


ACOES = [
    {"seq": 1, "acao": "criar", "ref": "novo:canaletas", "nome": "Canaletas e Acessórios", "pai": 10},
    {"seq": 2, "acao": "reparentar", "categoria": 21, "nome": "Contatores", "de": 10, "para": 20},
    {"seq": 3, "acao": "mover_produto", "produto": 1, "de": [10, 21], "para": ["novo:canaletas"]},
    {"seq": 4, "acao": "mover_produto", "produto": 3, "de": [40], "para": [21]},
    {"seq": 5, "acao": "arquivar_produto", "produto": 2, "payload": {"visibility": "hidden", "categories": []},
     "payload_fallback": {"published": False, "categories": []}},
    {"seq": 6, "acao": "verificar_vazia", "categoria": 30},
    {"seq": 7, "acao": "excluir", "categoria": 30},
]


@pytest.fixture
def loja():
    return LojaFake()


def _aplicar(loja, tmp_path, acoes=ACOES):
    return aplicar.aplicar_acoes(loja, acoes, plano_arquivo="p.json", snapshot_arquivo="s.json",
                                 log=lambda m: None, destino=tmp_path / "aplicacao-x.json")


def test_aplica_na_ordem_resolve_refs_e_registra_antes(loja, tmp_path):
    original = copy.deepcopy(loja.prods)
    diario = aplicar.carregar_diario(_aplicar(loja, tmp_path))
    assert diario["status"] == "concluida" and diario["ok"] == 7
    novo = diario["refs"]["novo:canaletas"]
    assert loja.prods[1]["categories"] == [novo]
    assert loja.cats[21]["parent"] == 20 and loja.cats[21]["handle"] == {"pt": "contatores"}
    rep = next(a for a in diario["acoes"] if a["acao"] == "reparentar")
    assert rep["handle_preservado"] is True and rep["antes"]["parent"] == 10
    assert loja.prods[2]["visibility"] == "hidden" and loja.prods[2]["categories"] == []
    assert 30 not in loja.cats
    mov = next(a for a in diario["acoes"] if a.get("produto") == 1)
    assert mov["antes"]["categories"] == original[1]["categories"]


def test_reverter_restaura_tudo_e_conferencia_passa(loja, tmp_path):
    estado_cats = {k: (v["name"]["pt"], v["parent"]) for k, v in loja.cats.items()}
    estado_prods = copy.deepcopy(loja.prods)
    ap = _aplicar(loja, tmp_path)
    rv = aplicar.reverter(ap, loja, log=lambda m: None)
    dados = json.loads(rv.read_text(encoding="utf-8"))
    assert dados["status"] == "concluida"
    novo_30 = dados["mapa_ids"]["30"]
    assert loja.cats[novo_30]["name"]["pt"] == "Vazia" and loja.cats[novo_30]["parent"] == 10
    assert loja.cats[novo_30]["handle"] == {"pt": "vazia"} and loja.cats[novo_30]["seo_title"] == "Vazia SEO"
    assert loja.cats[21]["parent"] == 10
    assert loja.prods == estado_prods  # categorias e visibilidade de volta
    criadas = [k for k in loja.cats if k not in estado_cats and k != novo_30]
    assert criadas == []  # categoria criada no piloto foi removida
    checks = aplicar.conferir_reversao(loja, ap, rv)
    assert checks and all(c["ok"] for c in checks), checks


def test_reversao_grava_o_produto_depois_de_restaurar_o_pai(loja, tmp_path):
    """A API real reinsere os ancestrais vigentes. Regravar o produto antes do reparent cola a categoria nova."""

    def com_ancestrais(pid, **campos):
        if "categories" in campos:
            vistos = []
            for cid in campos["categories"]:
                atual = cid
                while atual and atual not in vistos and atual in loja.cats:
                    vistos.append(atual)
                    atual = loja.cats[atual].get("parent")
            campos = {**campos, "categories": vistos}
        return LojaFake.atualizar_produto(loja, pid, **campos)

    loja.atualizar_produto = com_ancestrais
    acoes = [
        {"seq": 1, "acao": "criar", "ref": "novo:semicondutores", "nome": "Semicondutores", "pai": 10},
        {"seq": 2, "acao": "reparentar", "categoria": 21, "nome": "Contatores", "de": 10, "para": "novo:semicondutores"},
        {"seq": 3, "acao": "mover_produto", "produto": 1, "de": [10, 21], "para": [21, "novo:semicondutores", 10]},
    ]
    ap = _aplicar(loja, tmp_path, acoes)
    rv = aplicar.reverter(ap, loja, log=lambda m: None)
    dados = json.loads(rv.read_text(encoding="utf-8"))
    assert dados["status"] == "concluida", [(a["acao"], a["status"], a.get("erro")) for a in dados["acoes"]]
    assert sorted(loja.prods[1]["categories"]) == [10, 21]
    assert loja.cats[21]["parent"] == 10
    assert all(c["name"]["pt"] != "Semicondutores" for c in loja.cats.values())


def test_verificacao_bloqueia_exclusao_de_categoria_com_produto(loja, tmp_path):
    loja.prods[3]["categories"] = [30]
    diario = aplicar.carregar_diario(_aplicar(loja, tmp_path, [ACOES[5], ACOES[6]]))
    assert [a["status"] for a in diario["acoes"]] == ["falha", "pulada"]  # exclusão nem é tentada
    assert 30 in loja.cats and diario["status"] == "concluida_com_falhas"
    assert ("DELETE", 30) not in loja.chamadas


def test_verificacao_colada_a_exclusao_permite_excluir_pai_depois_das_filhas(loja, tmp_path):
    loja.cats[50] = {"id": 50, "name": {"pt": "Pai"}, "handle": {"pt": "pai"}, "parent": None, "subcategories": []}
    loja.cats[51] = {"id": 51, "name": {"pt": "Filha"}, "handle": {"pt": "filha"}, "parent": 50, "subcategories": []}
    loja._sync_subs()
    acoes = [{"seq": 1, "acao": "verificar_vazia", "categoria": 51}, {"seq": 2, "acao": "verificar_vazia", "categoria": 50},
             {"seq": 3, "acao": "excluir", "categoria": 51}, {"seq": 4, "acao": "excluir", "categoria": 50}]
    d = aplicar.carregar_diario(_aplicar(loja, tmp_path, acoes))
    assert [(a["seq"], a["status"]) for a in d["acoes"]] == [(1, "ok"), (3, "ok"), (2, "ok"), (4, "ok")]
    assert 50 not in loja.cats and 51 not in loja.cats


def test_excluir_orfa_so_quando_produtos_ja_estao_na_final_e_reverte(loja, tmp_path, monkeypatch):
    from wago.taxonomia import modelo

    loja.cats[60] = {"id": 60, "name": {"pt": "Avô antigo"}, "handle": {"pt": "avo"}, "parent": None, "subcategories": []}
    loja.prods[1]["categories"] = [60, 10, 21]  # final aprovada 21 + avô órfão 60
    loja.prods[3]["categories"] = [40]
    loja.listar_produtos = lambda campos=None, category_id=None: [loja._vista(p["id"]) for p in loja.prods.values()
                                                                  if category_id in p["categories"]]
    tax = {"nos": [{"id_alvo": "mat", "pai": None, "nuvemshop_id": 10}, {"id_alvo": "cont", "pai": "mat", "nuvemshop_id": 21},
                   {"id_alvo": "pap", "pai": None, "nuvemshop_id": 40}]}
    monkeypatch.setattr(modelo, "carregar_aprovada", lambda: tax)
    ap = aplicar.excluir_categorias_orfas(loja, [60, 10], log=lambda m: None, destino=tmp_path / "aplicacao-o.json")
    d = aplicar.carregar_diario(ap)
    assert [a["status"] for a in d["acoes"]] == ["ok", "falha"]  # 10 é aprovada: recusada
    assert 60 not in loja.cats and d["acoes"][0]["produtos_antes"] == {"1": [10, 21, 60]}
    loja.prods[1]["categories"] = [10, 21]  # a loja remove a categoria excluída dos produtos
    aplicar.reverter(ap, loja, log=lambda m: None)
    novo = next(k for k, v in loja.cats.items() if v["name"]["pt"] == "Avô antigo")
    assert sorted(loja.prods[1]["categories"]) == sorted([10, 21, novo])


def test_excluir_orfa_recusa_se_algum_produto_nao_tem_final(loja, tmp_path, monkeypatch):
    from wago.taxonomia import modelo

    loja.cats[60] = {"id": 60, "name": {"pt": "Avô"}, "handle": {"pt": "avo"}, "parent": None, "subcategories": []}
    loja.prods[3]["categories"] = [60]
    loja.listar_produtos = lambda campos=None, category_id=None: [loja._vista(p["id"]) for p in loja.prods.values()
                                                                  if category_id in p["categories"]]
    monkeypatch.setattr(modelo, "carregar_aprovada", lambda: {"nos": [{"id_alvo": "x", "pai": None, "nuvemshop_id": 21}]})
    d = aplicar.carregar_diario(aplicar.excluir_categorias_orfas(loja, [60], log=lambda m: None,
                                                                 destino=tmp_path / "aplicacao-o2.json"))
    assert d["acoes"][0]["status"] == "falha" and 60 in loja.cats


def test_falha_de_api_isola_a_acao_e_o_lote_continua(loja, tmp_path):
    original = loja.atualizar_produto

    def instavel(pid, **campos):
        if pid == 1:
            raise ErroNuvemshop(429, "limite/instabilidade da API (retries esgotados)")
        return original(pid, **campos)

    loja.atualizar_produto = instavel
    diario = aplicar.carregar_diario(_aplicar(loja, tmp_path))
    status = {a["seq"]: a["status"] for a in diario["acoes"]}
    assert status[3] == "falha" and "429" in next(a["erro"] for a in diario["acoes"] if a["seq"] == 3)
    assert status[4] == status[5] == status[7] == "ok"  # os próximos seguiram
    assert diario["falhas"] == 1 and diario["status"] == "concluida_com_falhas"


def test_criar_falhou_movimentos_para_ela_falham_sem_derrubar(loja, tmp_path):
    loja.criar_categoria = lambda *a, **k: (_ for _ in ()).throw(ErroNuvemshop(500, "erro interno"))
    diario = aplicar.carregar_diario(_aplicar(loja, tmp_path))
    status = {a["seq"]: a["status"] for a in diario["acoes"]}
    assert status[1] == "falha" and status[3] == "falha" and status[4] == "ok"


def test_retomar_pula_acoes_ja_concluidas(loja, tmp_path):
    original = loja.atualizar_produto
    estado = {"quebrar": True}

    def quebra_no_arquivo(pid, **campos):
        if pid == 2 and estado["quebrar"]:
            raise ErroNuvemshop(503, "indisponível")
        return original(pid, **campos)

    loja.atualizar_produto = quebra_no_arquivo
    primeiro = _aplicar(loja, tmp_path)
    estado["quebrar"] = False
    loja.chamadas.clear()
    segundo = aplicar.aplicar_acoes(loja, ACOES, plano_arquivo="p.json", snapshot_arquivo="s.json",
                                    log=lambda m: None, destino=tmp_path / "aplicacao-y.json", retomar=primeiro)
    d2 = aplicar.carregar_diario(segundo)
    assert [a["seq"] for a in d2["acoes"]] == [5]  # só o arquivamento que tinha falhado
    assert not any(c[0] == "POST" for c in loja.chamadas)
    assert loja.prods[2]["visibility"] == "hidden"


def test_estado_anterior_vem_do_snapshot_sem_get_extra(loja, tmp_path):
    gets = []
    original = loja.obter_produto
    loja.obter_produto = lambda pid: gets.append(pid) or original(pid)
    antes = {3: {"categories": [40], "published": True}}
    aplicar.aplicar_acoes(loja, [ACOES[3]], plano_arquivo="p", snapshot_arquivo="s", log=lambda m: None,
                          destino=tmp_path / "aplicacao-z.json", antes_produtos=antes)
    assert gets == []  # antes veio do snapshot e o PUT já confirmou as categorias
    d = aplicar.carregar_diario(tmp_path / "aplicacao-z.json")
    assert d["acoes"][0]["antes"] == {"categories": [40]}


def test_criar_reutilizada_nao_e_excluida_na_reversao(loja, tmp_path):
    acao = {"seq": 1, "acao": "criar", "ref": "novo:x", "nome": "Comutação", "pai": None}
    ap = _aplicar(loja, tmp_path, [acao])
    assert aplicar.carregar_diario(ap)["acoes"][0]["reutilizada"] is True
    aplicar.reverter(ap, loja, log=lambda m: None)
    assert 20 in loja.cats


def test_arquivar_revisao_cadastral_grava_o_motivo_no_diario(loja, tmp_path):
    acao = {**ACOES[4], "motivo": "Ocultado para revisão cadastral"}
    diario = aplicar.carregar_diario(_aplicar(loja, tmp_path, [acao]))
    assert diario["acoes"][0]["resumo"].startswith("Ocultado para revisão cadastral")
    assert loja.prods[2]["visibility"] == "hidden"


def test_arquivar_cai_no_fallback_published_em_422(loja, tmp_path):
    original = loja.atualizar_produto

    def sem_visibility(pid, **campos):
        if "visibility" in campos:
            raise ErroNuvemshop(422, "visibility not supported")
        return original(pid, **campos)

    loja.atualizar_produto = sem_visibility
    diario = aplicar.carregar_diario(_aplicar(loja, tmp_path, [ACOES[4]]))
    assert diario["acoes"][0]["campo"] == "published" and loja.prods[2]["published"] is False


def test_put_categoria_mescla_estado_atual_para_nao_apagar_nome():
    """Piloto real: PUT só com `parent` zerou o nome. O cliente reenvia os campos atuais."""
    from wago.taxonomia.nuvemshop_api import NuvemshopAPI

    enviados = []

    class Sessao:
        def request(self, metodo, url, headers=None, params=None, json=None, timeout=None):
            enviados.append((metodo, json))

            class R:
                status_code, headers, content = 200, {}, b"x"

                def json(self_inner):
                    return {"id": 21, "name": {"pt": "Contatores", "es": "Contactores"}, "handle": {"pt": "contatores"},
                            "parent": 10, "description": {"pt": ""}, "seo_title": {"pt": ""}}
            return R()

    api = NuvemshopAPI("123", "t", session=Sessao(), dormir=lambda s: None)
    api.atualizar_categoria(21, parent=20)
    metodo, corpo = enviados[-1]
    assert metodo == "PUT" and corpo["parent"] == 20
    assert corpo["name"] == {"pt": "Contatores", "es": "Contactores"} and corpo["handle"] == {"pt": "contatores"}
    assert "description" not in corpo and "seo_title" not in corpo  # {"pt": ""} → 422 na API real
    api.atualizar_categoria(21, name={"pt": "Contatores Tripolares"})
    assert enviados[-1][1]["name"] == {"pt": "Contatores Tripolares", "es": "Contactores"}
    assert enviados[-1][1]["parent"] == 10


def test_mover_falha_se_categoria_nao_aplicada(loja, tmp_path):
    loja.atualizar_produto = lambda pid, **c: loja.obter_produto(pid)  # API "aceita" mas não grava
    diario = aplicar.carregar_diario(_aplicar(loja, tmp_path, [ACOES[3]]))
    assert diario["acoes"][0]["status"] == "falha" and "não aplicadas" in diario["acoes"][0]["erro"]


def test_selecionar_piloto_escolhe_uma_de_cada():
    snap = {"categorias": [{"id": 21, "parent_id": 20}, {"id": 30, "parent_id": 10}, {"id": 20, "parent_id": None}],
            "produtos": [{"id": 1, "categorias": [21]}]}
    plano = {"fases": {
        "criar": [{"seq": 1, "acao": "criar", "ref": "novo:canaletas-e-acessorios", "nome": "Canaletas e Acessórios"}],
        "renomear": [], "reparentar": [{"seq": 2, "acao": "reparentar", "categoria": 21, "para": 10}],
        "mover": [{"seq": 3, "acao": "mover_produto", "produto": 1, "nome": "Canaleta 20x10",
                   "para": ["novo:canaletas-e-acessorios", 10]},
                  {"seq": 4, "acao": "mover_produto", "produto": 2, "para": [21]},
                  {"seq": 5, "acao": "mover_produto", "produto": 3, "para": [10]}],
        "arquivar": [{"seq": 6, "acao": "arquivar_produto", "nome": "Caderno", "fora_do_ramo": False},
                     {"seq": 7, "acao": "arquivar_produto", "nome": "Papel Sulfite A4", "fora_do_ramo": True}],
        "verificar": [{"seq": 8, "acao": "verificar_vazia", "categoria": 30}],
        "excluir": [{"seq": 9, "acao": "excluir", "categoria": 30, "motivo": "vazia"}],
    }}
    sel = aplicar.selecionar_piloto(plano, snap)
    assert [a["seq"] for a in sel] == [1, 2, 3, 4, 7, 8, 9]
