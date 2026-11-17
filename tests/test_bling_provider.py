import pytest

from wago.marketplaces.base import ErroAutenticacao, ErroRateLimit
from wago.marketplaces.bling_provider import BlingClient


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
