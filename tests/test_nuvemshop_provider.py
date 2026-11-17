"""Task 3 — NuvemshopProvider (adapter sobre o NuvemshopAPI existente) e paridade com aplicar.py."""

import pytest

from wago.marketplaces.base import ErroMarketplace
from wago.marketplaces.nuvemshop_provider import NuvemshopProvider, _publicado, _sku, traduzir_erro
from wago.taxonomia.nuvemshop_api import ErroNuvemshop, NuvemshopAPI


class Resp:
    def __init__(self, status, corpo=None, headers=None):
        self.status_code = status
        self._corpo = corpo
        self.headers = headers or {}
        self.content = b"x" if corpo is not None else b""
        self.text = ""

    def json(self):  # NuvemshopAPI.requisitar chama resp.json() quando há corpo
        return self._corpo


class FakeSession:
    """Registra método, URL e payload JSON de cada requisição."""

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def request(self, metodo, url, headers=None, params=None, json=None, timeout=None):
        self.chamadas.append({"metodo": metodo, "url": url, "json": json})
        return self.respostas.pop(0)


def _api(respostas):
    return NuvemshopAPI("123", "tok", session=FakeSession(respostas), dormir=lambda _s: None)


def test_publicado_hidden_vence_published_true():
    assert _publicado({"visibility": "hidden", "published": True}) is False


def test_publicado_sem_visibility_usa_published_como_fallback():
    assert _publicado({"published": False}) is False
    assert _publicado({}) is None


def test_sku_pega_a_primeira_variante_com_codigo():
    assert _sku({"variants": [{"sku": ""}, {"sku": " A-1 "}]}) == "A-1"
    assert _sku({}) == ""


def test_listar_categorias_converte_i18n_e_parent():
    api = _api([Resp(200, [{"id": 10, "name": {"pt": "Materiais"}, "parent": None, "description": {"pt": "Desc"}}])])
    assert NuvemshopProvider(api).listar_categorias() == [
        {"id": 10, "nome": "Materiais", "pai_id": None, "descricao": "Desc"}
    ]


def test_listar_produtos_mapeia_sku_categorias_e_publicado():
    api = _api([Resp(200, [{
        "id": 1, "name": {"pt": "Contator"}, "categories": [{"id": 21}, 10],
        "variants": [{"sku": "SKU-1"}], "visibility": "hidden", "published": True,
    }])])
    assert NuvemshopProvider(api).listar_produtos() == [
        {"id": 1, "nome": "Contator", "sku": "SKU-1", "categorias": [21, 10], "publicado": False}
    ]


def test_criar_categoria_repassa_descricao_e_marca_reutilizada():
    api = _api([Resp(201, {"id": 33, "name": {"pt": "Canaletas"}, "parent": 10, "description": {"pt": "d"}})])
    criada = NuvemshopProvider(api).criar_categoria({"nome": "Canaletas", "pai_id": 10, "descricao": "d"})
    assert criada["reutilizada"] is False
    assert criada["pai_id"] == 10
    assert api.session.chamadas[0]["json"]["description"] == {"pt": "d"}


def test_atualizar_categoria_envia_name_i18n_e_parent():
    api = _api([
        Resp(200, {"id": 33, "name": {"pt": "Antigo"}, "parent": None}),
        Resp(200, {"id": 33, "name": {"pt": "Novo"}, "parent": 10}),
    ])
    NuvemshopProvider(api).atualizar_categoria(33, {"nome": "Novo", "pai_id": 10})
    enviado = api.session.chamadas[1]["json"]
    assert enviado["name"] == {"pt": "Novo"} and enviado["parent"] == 10


def test_deletar_categoria_404_e_idempotente():
    api = _api([Resp(404, {"description": "Not Found"})])
    NuvemshopProvider(api).deletar_categoria(99)  # não levanta


def test_atualizar_produto_move_categorias():
    api = _api([Resp(200, {"id": 1, "categories": [{"id": 21}], "published": True, "visibility": "visible"})])
    NuvemshopProvider(api).atualizar_produto(1, {"categorias": [21]})
    assert api.session.chamadas[0]["json"]["categories"] == [21]


def test_atualizar_produto_arquiva_com_visibility_e_cai_para_published_em_422():
    api = _api([
        Resp(422, {"description": "invalid visibility"}),
        Resp(200, {"id": 1, "published": False, "visibility": "hidden"}),
    ])
    produto = NuvemshopProvider(api).atualizar_produto(1, {"publicado": False})
    assert api.session.chamadas[0]["json"]["visibility"] == "hidden"
    assert api.session.chamadas[1]["json"]["published"] is False
    assert produto["publicado"] is False
    # paridade com `_executar`/arquivar_produto: visibility primeiro, sem GET de confirmação
    assert [c["metodo"] for c in api.session.chamadas] == ["PUT", "PUT"]


def test_atualizar_produto_renomeia_preservando_os_outros_idiomas():
    api = _api([
        Resp(200, {"id": 1, "name": {"pt": "Antigo", "es": "Viejo", "en": "Old"}}),
        Resp(200, {"id": 1, "name": {"pt": "Novo", "es": "Viejo", "en": "Old"}}),
    ])
    NuvemshopProvider(api).atualizar_produto(1, {"nome": "Novo"})
    assert api.session.chamadas[1]["json"]["name"] == {"pt": "Novo", "es": "Viejo", "en": "Old"}
    # paridade com o rename de produto: GET antes do PUT para mesclar o name i18n
    assert [c["metodo"] for c in api.session.chamadas] == ["GET", "PUT"]


def test_atualizar_produto_recusa_sku_nivel_produto():
    api = _api([])
    with pytest.raises(ErroMarketplace) as exc:
        NuvemshopProvider(api).atualizar_produto(1, {"sku": "NOVO"})
    assert exc.value.status == 400


# ---------------------------------------------------------------- adendos do parceiro humano

def test_paridade_criar_categoria_payload_identico_ao_fluxo_atual():
    resposta = {"id": 33, "name": {"pt": "Canaletas"}, "parent": 10,
                "description": {"pt": "Canaletas"}}
    api_atual = _api([Resp(201, resposta)])
    api_adapter = _api([Resp(201, resposta)])

    api_atual.criar_categoria("Canaletas", 10)  # chamada do aplicar.py
    NuvemshopProvider(api_adapter).criar_categoria(
        {"nome": "Canaletas", "pai_id": 10, "descricao": ""}
    )

    atual = api_atual.session.chamadas[0]
    adaptado = api_adapter.session.chamadas[0]
    assert (adaptado["metodo"], adaptado["url"]) == (atual["metodo"], atual["url"])
    assert adaptado["json"] == atual["json"]
    assert adaptado["json"]["name"] == {"pt": "Canaletas"}
    assert adaptado["json"]["parent"] == 10


def test_criar_categoria_422_marca_reutilizada_quando_nome_conflita():
    api = _api([
        Resp(422, {"description": "name already exists"}),
        Resp(200, [{"id": 33, "name": {"pt": "Canaletas"}, "parent": 10}]),
    ])
    criada = NuvemshopProvider(api).criar_categoria(
        {"nome": "Canaletas", "pai_id": 10, "descricao": ""}
    )
    assert criada["reutilizada"] is True
    assert criada["id"] == 33
    assert api.session.chamadas[0]["metodo"] == "POST"
    assert api.session.chamadas[1]["metodo"] == "GET"


@pytest.mark.parametrize("status,esperado", [
    (401, "ErroAutenticacao"), (403, "ErroAutenticacao"), (404, "NaoEncontrado"),
    (429, "ErroRateLimit"), (422, "ErroMarketplace"), (500, "ErroMarketplace"),
])
def test_traduzir_erro_copia_status_mensagem_e_corpo(status, esperado):
    exc = ErroNuvemshop(status, "mensagem original", {"description": "x"})
    traduzido = traduzir_erro(exc)
    assert type(traduzido).__name__ == esperado
    assert traduzido.status == status
    assert traduzido.corpo == {"description": "x"}
    assert str(exc) in traduzido.mensagem
    assert isinstance(traduzido, ErroMarketplace)
