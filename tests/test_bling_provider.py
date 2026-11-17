import pytest

from wago.marketplaces.base import (
    CategoriaComProdutos,
    ErroAutenticacao,
    ErroMarketplace,
    ErroRateLimit,
    NaoEncontrado,
)
from wago.marketplaces.bling_provider import BlingClient, BlingProvider, _ids_produto


class Resp:
    def __init__(self, status, corpo=None, headers=None):
        self.status_code = status
        self._corpo = corpo
        self.headers = headers or {}
        self.content = b"x" if corpo is not None else b""
        self.text = ""

    def json(self):  # o cliente lê resp.json() quando há corpo
        return self._corpo


class Sessao:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def request(self, metodo, url, headers=None, params=None, json=None, timeout=None):
        self.chamadas.append({"metodo": metodo, "url": url, "headers": headers, "params": params, "json": json})
        return self.respostas.pop(0)


class Auth:
    def __init__(self):
        self.renovacoes = 0

    def access_token(self):
        return "tok"

    def renovar(self):
        self.renovacoes += 1
        return {"access_token": "novo"}


class Limiter:
    def __init__(self):
        self.adquiridas = 0
        self.esperas = []

    def adquirir(self):
        self.adquiridas += 1

    def aguardar_429(self, headers, tentativa):
        self.esperas.append((dict(headers), tentativa))
        return 0.0


def _client(respostas):
    sessao, auth, limiter = Sessao(respostas), Auth(), Limiter()
    return BlingClient(auth, session=sessao, limiter=limiter), sessao, auth, limiter


def test_requisitar_inclui_bearer_e_desembrulha_data():
    client, sessao, _auth, _limiter = _client([Resp(200, {"data": [{"id": 1}]})])
    assert client.requisitar("GET", "/categorias/produtos") == [{"id": 1}]
    assert sessao.chamadas[0]["headers"]["Authorization"] == "Bearer tok"


def test_requisitar_renova_uma_vez_em_401():
    client, sessao, auth, _limiter = _client([Resp(401, {"error": "expired"}), Resp(200, {"data": []})])
    assert client.requisitar("GET", "/produtos") == []
    assert auth.renovacoes == 1
    assert len(sessao.chamadas) == 2


def test_requisitar_401_persistente_levanta_erro_de_autenticacao():
    client, _sessao, auth, _limiter = _client([Resp(401, {}), Resp(401, {})])
    with pytest.raises(ErroAutenticacao):
        client.requisitar("GET", "/produtos")
    assert auth.renovacoes == 1


def test_requisitar_429_respeita_retry_after_e_repete():
    client, sessao, _auth, limiter = _client([
        Resp(429, {}, {"Retry-After": "2"}),
        Resp(200, {"data": [1]}),
    ])
    assert client.requisitar("GET", "/produtos") == [1]
    assert limiter.esperas == [({"Retry-After": "2"}, 1)]
    assert len(sessao.chamadas) == 2


def test_requisitar_429_persistente_levanta_erro_rate_limit_apos_5_tentativas():
    client, sessao, _auth, _limiter = _client([Resp(429, {}) for _ in range(5)])
    with pytest.raises(ErroRateLimit):
        client.requisitar("GET", "/produtos")
    assert len(sessao.chamadas) == 5


def test_paginar_para_quando_pagina_vem_menor_que_o_limite():
    client, sessao, _auth, _limiter = _client([
        Resp(200, {"data": [{"id": i} for i in range(100)]}),
        Resp(200, {"data": [{"id": 100}]}),
    ])
    itens = list(client.paginar("/produtos"))
    assert len(itens) == 101
    assert sessao.chamadas[0]["params"] == {"pagina": 1, "limite": 100}
    assert sessao.chamadas[1]["params"] == {"pagina": 2, "limite": 100}


def test_paginar_trata_data_null_como_fim():
    client, _sessao, _auth, _limiter = _client([Resp(200, {"data": None})])
    assert list(client.paginar("/produtos")) == []


def test_requisitar_aceita_lista_direta_sem_envelope_data():
    client, _sessao, _auth, _limiter = _client([Resp(200, [{"id": 1}])])
    assert client.requisitar("GET", "/produtos") == [{"id": 1}]


class ClienteFake:
    def __init__(self, paginas=(), respostas=()):
        self.paginas = list(paginas)
        self.respostas = list(respostas)
        self.chamadas = []

    def paginar(self, caminho, params=None):
        self.chamadas.append(("GET", caminho, params, None))
        yield from self.paginas.pop(0)

    def requisitar(self, metodo, caminho, *, params=None, json=None):
        self.chamadas.append((metodo, caminho, params, json))
        resposta = self.respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


def test_listar_categorias_mapeia_nome_pai_e_descricao_vazia():
    client = ClienteFake(paginas=[[
        {"id": 1, "descricao": "Automação", "categoriaPaiId": None},
        {"id": 2, "descricao": "CLPs", "categoriaPaiId": 1},
    ]])
    assert BlingProvider(client).listar_categorias() == [
        {"id": 1, "nome": "Automação", "pai_id": None, "descricao": ""},
        {"id": 2, "nome": "CLPs", "pai_id": 1, "descricao": ""},
    ]


def test_listar_categorias_sem_produtos_devolve_lista_vazia():
    assert BlingProvider(ClienteFake(paginas=[[]])).listar_categorias() == []


def test_ids_produto_une_categoria_e_categorias_sem_duplicar():
    assert _ids_produto({"categoria": {"id": 5}, "categorias": [{"id": 5}, {"id": 6}]}) == [5, 6]
    assert _ids_produto({}) == []


def test_listar_produtos_mapeia_campos_ausentes_para_vazio():
    client = ClienteFake(paginas=[[
        {"id": 7, "nome": "Produto", "codigo": "SKU-1", "categoria": {"id": 5}},
        {"id": 8},
    ]])
    assert BlingProvider(client).listar_produtos() == [
        {"id": 7, "nome": "Produto", "sku": "SKU-1", "categorias": [5], "publicado": None},
        {"id": 8, "nome": "", "sku": "", "categorias": [], "publicado": None},
    ]


def test_criar_categoria_sem_pai_nao_envia_categoria_pai_id():
    client = ClienteFake(respostas=[{"id": 9, "descricao": "Nova"}])
    criada = BlingProvider(client).criar_categoria({"nome": "Nova", "pai_id": None, "descricao": ""})
    assert client.chamadas[0] == ("POST", "/categorias/produtos", None, {"descricao": "Nova"})
    assert criada["reutilizada"] is False and criada["nome"] == "Nova"


def test_criar_categoria_com_pai_envia_categoria_pai_id():
    client = ClienteFake(respostas=[{"id": 9, "descricao": "Filha", "categoriaPaiId": 1}])
    BlingProvider(client).criar_categoria({"nome": "Filha", "pai_id": 1, "descricao": ""})
    assert client.chamadas[0][3] == {"descricao": "Filha", "categoriaPaiId": 1}


def test_criar_categoria_recusa_descricao_diferente_do_nome():
    client = ClienteFake()
    with pytest.raises(ErroMarketplace) as exc:
        BlingProvider(client).criar_categoria({"nome": "Nova", "pai_id": None, "descricao": "Texto"})
    assert exc.value.status == 400 and client.chamadas == []


def test_atualizar_categoria_envia_somente_campos_alterados():
    client = ClienteFake(respostas=[{"id": 9, "descricao": "Renomeada"}])
    BlingProvider(client).atualizar_categoria(9, {"nome": "Renomeada"})
    assert client.chamadas[0] == ("PUT", "/categorias/produtos/9", None, {"descricao": "Renomeada"})


def test_atualizar_categoria_recusa_descricao():
    client = ClienteFake()
    with pytest.raises(ErroMarketplace) as exc:
        BlingProvider(client).atualizar_categoria(9, {"descricao": "x"})
    assert exc.value.status == 400 and client.chamadas == []


def test_deletar_categoria_bloqueia_quando_ha_produto_vinculado():
    client = ClienteFake(respostas=[[{"id": 7}]])
    with pytest.raises(CategoriaComProdutos):
        BlingProvider(client).deletar_categoria(5)
    assert all(chamada[0] != "DELETE" for chamada in client.chamadas)


def test_deletar_categoria_quando_filtro_recusado_tenta_delete():
    client = ClienteFake(respostas=[ErroMarketplace(400, "filtro inválido"), None])
    BlingProvider(client).deletar_categoria(5)
    assert client.chamadas[1][0] == "DELETE"


def test_deletar_categoria_inexistente_e_idempotente():
    client = ClienteFake(respostas=[[], NaoEncontrado(404, "não existe")])
    BlingProvider(client).deletar_categoria(5)  # não levanta


def test_atualizar_produto_uma_categoria_envia_categoria_singular_e_preserva_campos():
    atual = {"id": 7, "nome": "Antigo", "descricao": "Descrição", "categoria": {"id": 1}}
    client = ClienteFake(respostas=[atual, {"id": 7, **{k: v for k, v in atual.items() if k != "id"}, "nome": "Novo", "categoria": {"id": 2}}])
    produto = BlingProvider(client).atualizar_produto(7, {"categorias": [2], "nome": "Novo"})
    assert client.chamadas[0][0] == "GET" and client.chamadas[1][0] == "PUT"
    enviado = client.chamadas[1][3]
    assert enviado["nome"] == "Novo" and enviado["descricao"] == "Descrição"
    assert enviado["categoria"] == {"id": 2} and "categorias" not in enviado and "id" not in enviado
    assert produto["categorias"] == [2] and produto["publicado"] is None


def test_atualizar_produto_varias_categorias_envia_categorias_plural():
    client = ClienteFake(respostas=[{"id": 7, "nome": "P", "categoria": {"id": 1}}, {"id": 7, "nome": "P"}])
    BlingProvider(client).atualizar_produto(7, {"categorias": [2, 3]})
    enviado = client.chamadas[1][3]
    assert enviado["categorias"] == [{"id": 2}, {"id": 3}] and "categoria" not in enviado


def test_atualizar_produto_com_lista_vazia_remove_categorias():
    client = ClienteFake(respostas=[{"id": 7, "nome": "P", "categoria": {"id": 1}}, {"id": 7, "nome": "P"}])
    BlingProvider(client).atualizar_produto(7, {"categorias": []})
    enviado = client.chamadas[1][3]
    assert enviado["categorias"] == [] and "categoria" not in enviado


def test_atualizar_produto_recusa_publicado():
    client = ClienteFake()
    with pytest.raises(ErroMarketplace) as exc:
        BlingProvider(client).atualizar_produto(7, {"publicado": False})
    assert exc.value.status == 400 and client.chamadas == []
