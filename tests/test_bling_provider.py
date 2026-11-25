import pytest
import requests

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

    def adquirir(self, **kwargs):
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


def test_requisitar_429_pausa_sem_retry_nem_espera():
    client, sessao, _auth, limiter = _client([
        Resp(429, {}, {"Retry-After": "2"}),
        Resp(200, {"data": [1]}),
    ])
    with pytest.raises(ErroRateLimit):
        client.requisitar("GET", "/produtos")
    assert limiter.esperas == []
    assert len(sessao.chamadas) == 1


def test_requisitar_429_persistente_levanta_erro_na_primeira_tentativa():
    client, sessao, _auth, _limiter = _client([Resp(429, {}) for _ in range(5)])
    with pytest.raises(ErroRateLimit):
        client.requisitar("GET", "/produtos")
    assert len(sessao.chamadas) == 1


def test_429_bloqueia_novos_clientes_ate_o_dia_seguinte(tmp_path):
    from wago.marketplaces.rate_limit import RateLimiter

    hoje = ["2026-11-25"]
    kwargs = {"banco": tmp_path / "bling.sqlite", "dia": lambda: hoje[0], "dormir": lambda _: None}
    sessao = Sessao([Resp(429, {}), Resp(200, {"data": []})])
    client = BlingClient(Auth(), session=sessao, limiter=RateLimiter(**kwargs))
    with pytest.raises(ErroRateLimit):
        client.requisitar("GET", "/produtos")
    outro = BlingClient(Auth(), session=sessao, limiter=RateLimiter(**kwargs))
    with pytest.raises(ErroRateLimit):
        outro.requisitar("GET", "/produtos")
    assert len(sessao.chamadas) == 1
    hoje[0] = "2026-11-26"
    assert outro.requisitar("GET", "/produtos") == []
    assert len(sessao.chamadas) == 2


def test_retry_5xx_e_contado_no_orcamento_persistente(tmp_path):
    from wago.marketplaces.rate_limit import RateLimiter

    limiter = RateLimiter(banco=tmp_path / "bling.sqlite", dormir=lambda _: None)
    client = BlingClient(Auth(), session=Sessao([Resp(503, {}), Resp(204)]), limiter=limiter)
    client.requisitar("PUT", "/produtos/1", json={"nome": "Sensor"})
    consumo = RateLimiter(banco=tmp_path / "bling.sqlite").consumo()
    assert (consumo["total"], consumo["escritas"], consumo["retries"]) == (2, 2, 1)


def test_requisitar_5xx_instabilidade_repete_e_levanta_erro_marketplace():
    client, sessao, _auth, _limiter = _client([Resp(503, {"error": "boom"}) for _ in range(5)])
    with pytest.raises(ErroMarketplace) as exc:
        client.requisitar("GET", "/produtos")
    assert exc.value.status == 503 and len(sessao.chamadas) == 5


def test_requisitar_5xx_depois_de_sucesso_repete_e_retorna():
    client, sessao, _auth, _limiter = _client([Resp(503, {}), Resp(200, {"data": [1]})])
    assert client.requisitar("GET", "/produtos") == [1]
    assert len(sessao.chamadas) == 2


def test_requisitar_falha_de_rede_levanta_erro_marketplace_sem_traceback():
    class SessaoRede:
        def request(self, *args, **kwargs):
            raise requests.ConnectionError("conectao recusada")

    client = BlingClient(Auth(), session=SessaoRede(), limiter=Limiter())
    with pytest.raises(ErroMarketplace) as exc:
        client.requisitar("GET", "/produtos")
    assert exc.value.status == 0 and "falha de rede" in str(exc.value)
    assert "ConnectionError" in str(exc.value)


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


def test_paginar_preserva_params_extras():
    client, sessao, _auth, _limiter = _client([Resp(200, {"data": []})])
    assert list(client.paginar("/produtos", {"idCategoria": 5})) == []
    assert sessao.chamadas[0]["params"] == {"idCategoria": 5, "pagina": 1, "limite": 100}


def test_requisitar_aceita_lista_direta_sem_envelope_data():
    client, _sessao, _auth, _limiter = _client([Resp(200, [{"id": 1}])])
    assert client.requisitar("GET", "/produtos") == [{"id": 1}]


class ClienteFake:
    """`paginas` pode ser lista (uma fila de páginas) ou dict indexado por
    `(caminho, idCategoria)` — necessário para o mapa de categorias de produtos."""

    def __init__(self, paginas=(), respostas=()):
        self.paginas = paginas if isinstance(paginas, dict) else list(paginas)
        self.respostas = list(respostas)
        self.chamadas = []

    def paginar(self, caminho, params=None):
        self.chamadas.append(("GET", caminho, params, None))
        if isinstance(self.paginas, dict):
            fila = self.paginas[(caminho, (params or {}).get("idCategoria"))]
        else:
            fila = self.paginas
        yield from fila.pop(0)

    def requisitar(self, metodo, caminho, *, params=None, json=None):
        self.chamadas.append((metodo, caminho, params, json))
        resposta = self.respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


def test_listar_categorias_mapeia_nome_pai_e_descricao_vazia():
    # formato real da API v3: pai como objeto, id 0 na raiz
    client = ClienteFake(paginas=[[
        {"id": 1, "descricao": "Automação", "categoriaPai": {"id": 0}},
        {"id": 2, "descricao": "CLPs", "categoriaPai": {"id": 1}},
    ]])
    assert BlingProvider(client).listar_categorias() == [
        {"id": 1, "nome": "Automação", "pai_id": None, "descricao": ""},
        {"id": 2, "nome": "CLPs", "pai_id": 1, "descricao": ""},
    ]


def test_listar_categorias_aceita_categoria_pai_id_plano_legado():
    client = ClienteFake(paginas=[[{"id": 2, "descricao": "CLPs", "categoriaPaiId": 1}]])
    assert BlingProvider(client).listar_categorias()[0]["pai_id"] == 1


def test_listar_categorias_sem_produtos_devolve_lista_vazia():
    assert BlingProvider(ClienteFake(paginas=[[]])).listar_categorias() == []


def test_ids_produto_une_categoria_e_categorias_sem_duplicar():
    assert _ids_produto({"categoria": {"id": 5}, "categorias": [{"id": 5}, {"id": 6}]}) == [5, 6]
    assert _ids_produto({}) == []


def test_listar_produtos_mapeia_campos_ausentes_para_vazio():
    client = ClienteFake(paginas={
        ("/produtos", None): [[
            {"id": 7, "nome": "Produto", "codigo": "SKU-1"},
            {"id": 8},
        ]],
        ("/categorias/produtos", None): [[{"id": 5, "descricao": "Categoria"}]],
        ("/produtos", 5): [[{"id": 7, "nome": "Produto", "codigo": "SKU-1"}]],
    })
    assert BlingProvider(client).listar_produtos() == [
        {"id": 7, "nome": "Produto", "sku": "SKU-1", "categorias": [5], "publicado": None},
        {"id": 8, "nome": "", "sku": "", "categorias": [], "publicado": None},
    ]


def test_listar_produtos_une_categorias_de_produto_em_varias_listas():
    client = ClienteFake(paginas={
        ("/produtos", None): [[{"id": 1, "nome": "P1"}]],
        ("/categorias/produtos", None): [[
            {"id": 10, "descricao": "A"},
            {"id": 20, "descricao": "B"},
        ]],
        ("/produtos", 10): [[{"id": 1, "nome": "P1"}]],
        ("/produtos", 20): [[{"id": 1, "nome": "P1"}, {"id": 2, "nome": "P2"}]],
    })
    produtos = BlingProvider(client).listar_produtos()
    assert produtos == [
        {"id": 1, "nome": "P1", "sku": "", "categorias": [10, 20], "publicado": None},
        {"id": 2, "nome": "P2", "sku": "", "categorias": [20], "publicado": None},
    ]
    assert ("GET", "/produtos", {"idCategoria": 10}, None) in client.chamadas


def test_criar_categoria_sem_pai_nao_envia_categoria_pai():
    client = ClienteFake(respostas=[
        {"id": 9},  # POST esparso
        {"id": 9, "descricao": "Nova", "categoriaPai": {"id": 0}},  # GET confirma
    ])
    criada = BlingProvider(client).criar_categoria({"nome": "Nova", "pai_id": None, "descricao": ""})
    assert client.chamadas[0] == ("POST", "/categorias/produtos", None, {"descricao": "Nova"})
    assert criada["reutilizada"] is False and criada["nome"] == "Nova"


def test_criar_categoria_com_pai_envia_categoria_pai_objeto():
    client = ClienteFake(respostas=[
        {"id": 9},  # POST esparso
        {"id": 9, "descricao": "Filha", "categoriaPai": {"id": 1}},  # GET confirma
    ])
    criada = BlingProvider(client).criar_categoria({"nome": "Filha", "pai_id": 1, "descricao": ""})
    # a API real ignora `categoriaPaiId` e aplica somente o objeto
    assert client.chamadas[0][3] == {"descricao": "Filha", "categoriaPai": {"id": 1}}
    assert criada["pai_id"] == 1


def test_criar_categoria_confere_pai_aplicado_por_get():
    client = ClienteFake(respostas=[
        {"id": 9},
        {"id": 9, "descricao": "Filha", "categoriaPai": {"id": 0}},  # pai não aplicou
    ])
    with pytest.raises(ErroMarketplace) as exc:
        BlingProvider(client).criar_categoria({"nome": "Filha", "pai_id": 1, "descricao": ""})
    assert exc.value.status == 409 and "pai não aplicado" in str(exc.value)


def test_criar_categoria_recusa_descricao_diferente_do_nome():
    client = ClienteFake()
    with pytest.raises(ErroMarketplace) as exc:
        BlingProvider(client).criar_categoria({"nome": "Nova", "pai_id": None, "descricao": "Texto"})
    assert exc.value.status == 400 and client.chamadas == []


def test_atualizar_categoria_envia_somente_campos_alterados():
    client = ClienteFake(respostas=[
        {"id": 9, "descricao": "Antiga", "categoriaPai": {"id": 0}},  # GET atual
        None,                                                       # PUT 204
        {"id": 9, "descricao": "Renomeada", "categoriaPai": {"id": 0}},  # GET confirma
    ])
    cat = BlingProvider(client).atualizar_categoria(9, {"nome": "Renomeada"})
    assert client.chamadas[1] == ("PUT", "/categorias/produtos/9", None, {"descricao": "Renomeada"})
    assert cat["nome"] == "Renomeada"


def test_atualizar_categoria_recusa_descricao():
    client = ClienteFake()
    with pytest.raises(ErroMarketplace) as exc:
        BlingProvider(client).atualizar_categoria(9, {"descricao": "x"})
    assert exc.value.status == 400 and client.chamadas == []


def test_atualizar_categoria_recusa_reparent_sem_put():
    # a API real ignora o pai no PUT: falhar explícito evita no-op silencioso
    client = ClienteFake(respostas=[{"id": 9, "descricao": "C", "categoriaPai": {"id": 5}}])
    with pytest.raises(ErroMarketplace) as exc:
        BlingProvider(client).atualizar_categoria(9, {"pai_id": 6})
    assert exc.value.status == 400 and "não permite mover" in str(exc.value)
    assert [c[0] for c in client.chamadas] == ["GET"]


def test_atualizar_categoria_pai_igual_ao_atual_so_renomeia():
    client = ClienteFake(respostas=[
        {"id": 9, "descricao": "C", "categoriaPai": {"id": 5}},
        None,
        {"id": 9, "descricao": "C2", "categoriaPai": {"id": 5}},
    ])
    cat = BlingProvider(client).atualizar_categoria(9, {"nome": "C2", "pai_id": "5"})
    assert client.chamadas[1][3] == {"descricao": "C2"}
    assert cat["pai_id"] == 5


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


def test_deletar_categoria_400_no_delete_vira_categoria_com_produtos_com_mensagem_original():
    client = ClienteFake(respostas=[[], ErroMarketplace(400, "categoria possui produtos")])
    with pytest.raises(CategoriaComProdutos) as exc:
        BlingProvider(client).deletar_categoria(5)
    assert exc.value.status == 409
    assert "categoria possui produtos" in str(exc.value)


def test_atualizar_produto_uma_categoria_envia_categoria_singular_e_preserva_campos():
    atual = {"id": 7, "nome": "Antigo", "descricao": "Descrição", "categoria": {"id": 1}}
    client = ClienteFake(respostas=[atual, {"id": 7, **{k: v for k, v in atual.items() if k != "id"}, "nome": "Novo", "categoria": {"id": 2}}])
    produto = BlingProvider(client).atualizar_produto(7, {"categorias": [2], "nome": "Novo"})
    assert client.chamadas[0][0] == "GET" and client.chamadas[1][0] == "PUT"
    enviado = client.chamadas[1][3]
    assert enviado["nome"] == "Novo" and enviado["descricao"] == "Descrição"
    assert enviado["categoria"] == {"id": 2} and "categorias" not in enviado and "id" not in enviado
    # 'publicado' agora é derivado de 'situacao' (padrão "A" = ativo)
    assert produto["categorias"] == [2] and produto["publicado"] is True


def test_atualizar_produto_varias_categorias_envia_categorias_plural():
    client = ClienteFake(respostas=[{"id": 7, "nome": "P", "categoria": {"id": 1}}, {"id": 7, "nome": "P"}])
    produto = BlingProvider(client).atualizar_produto(7, {"categorias": [2, 3]})
    enviado = client.chamadas[1][3]
    assert enviado["categorias"] == [{"id": 2}, {"id": 3}] and "categoria" not in enviado
    assert produto["categorias"] == [2, 3]


def test_atualizar_produto_com_lista_vazia_remove_categorias():
    client = ClienteFake(respostas=[{"id": 7, "nome": "P", "categoria": {"id": 1}}, {"id": 7, "nome": "P"}])
    produto = BlingProvider(client).atualizar_produto(7, {"categorias": []})
    enviado = client.chamadas[1][3]
    assert enviado["categorias"] == [] and "categoria" not in enviado
    assert produto["categorias"] == []


def test_atualizar_produto_uma_categoria_nao_herda_categorias_antigas():
    atual = {"id": 7, "nome": "P", "categorias": [{"id": 1}, {"id": 2}]}
    client = ClienteFake(respostas=[atual, {"id": 7, "nome": "P"}])
    produto = BlingProvider(client).atualizar_produto(7, {"categorias": [3]})
    assert client.chamadas[1][3]["categoria"] == {"id": 3}
    assert produto["categorias"] == [3]


def test_atualizar_produto_publicado_mapeia_para_situacao():
    # 'publicado' é um alias canônico de 'situacao': False => "I" (inativo), True => "A" (ativo)
    atual = {"id": 7, "nome": "P"}
    client = ClienteFake(respostas=[atual, {"id": 7, "nome": "P", "situacao": "I"}])
    produto = BlingProvider(client).atualizar_produto(7, {"publicado": False})
    enviado = client.chamadas[1][3]
    assert enviado["situacao"] == "I"
    assert produto["publicado"] is False
