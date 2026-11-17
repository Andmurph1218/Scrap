# tests/test_marketplaces_base.py
import pytest

from wago.marketplaces.base import (
    AlteracaoCategoria, AlteracaoProduto, BaseMarketplaceProvider, Categoria,
    CategoriaComProdutos, ErroAutenticacao, ErroMarketplace, ErroRateLimit, NaoEncontrado, NovaCategoria, Produto,
)


class Completo(BaseMarketplaceProvider):
    nome = "fake"

    def listar_categorias(self): return []
    def criar_categoria(self, dados): return {"id": 1, "nome": dados["nome"], "pai_id": None, "descricao": ""}
    def atualizar_categoria(self, id, dados): return {"id": id, "nome": "x", "pai_id": None, "descricao": ""}
    def deletar_categoria(self, id): return None
    def listar_produtos(self): return []
    def atualizar_produto(self, id, dados): return {"id": id, "nome": "x", "sku": "", "categorias": [], "publicado": None}


def test_provider_incompleto_nao_instancia():
    class Incompleto(BaseMarketplaceProvider):
        pass

    with pytest.raises(TypeError):
        Incompleto()


def test_provider_completo_instancia_e_expoe_nome():
    assert Completo().nome == "fake"


@pytest.mark.parametrize("tipo", [ErroAutenticacao, ErroRateLimit, CategoriaComProdutos, NaoEncontrado])
def test_erros_derivam_de_erro_marketplace_com_status_e_corpo(tipo):
    exc = tipo(401, "sem token", {"erro": "x"})
    assert isinstance(exc, ErroMarketplace)
    assert exc.status == 401
    assert exc.corpo == {"erro": "x"}
    assert str(exc) == "HTTP 401: sem token"


def test_typed_dicts_tem_as_chaves_do_spec():
    assert Categoria.__required_keys__ == frozenset({"id", "nome", "pai_id", "descricao"})
    assert NovaCategoria.__required_keys__ == frozenset({"nome"})
    assert NovaCategoria.__optional_keys__ == frozenset({"pai_id", "descricao"})
    assert Produto.__required_keys__ == frozenset({"id", "nome", "sku", "categorias", "publicado"})
    assert AlteracaoCategoria.__optional_keys__ == frozenset({"nome", "pai_id", "descricao"})
    assert AlteracaoProduto.__optional_keys__ == frozenset({"categorias", "publicado", "nome"})
