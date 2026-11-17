import json

import pytest

from wago.marketplaces.base import ErroAutenticacao
from wago.marketplaces.bling_auth import BlingAuth, TokenStore


class Resp:
    def __init__(self, status, corpo=None):
        self.status_code = status
        self._corpo = corpo
        self.content = b"x" if corpo is not None else b""
        self.text = json.dumps(corpo or {})

    def json(self):
        return self._corpo


class Sessao:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def post(self, url, data=None, auth=None, timeout=None):
        self.chamadas.append({"url": url, "data": dict(data or {}), "auth": auth})
        return self.respostas.pop(0)


@pytest.fixture
def store(tmp_path):
    return TokenStore(tmp_path / "bling_tokens.json")


@pytest.fixture
def credenciais(monkeypatch):
    monkeypatch.setenv("BLING_CLIENT_ID", "cid")
    monkeypatch.setenv("BLING_CLIENT_SECRET", "segredo")
    monkeypatch.delenv("BLING_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BLING_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("BLING_REDIRECT_URI", raising=False)


def test_salvar_e_carregar_tokens_grava_atualizado_em(store):
    store.salvar({"access_token": "a", "refresh_token": "r", "expira_em": 123})
    tokens = store.carregar()
    assert tokens["access_token"] == "a" and tokens["refresh_token"] == "r"
    assert "atualizado_em" in tokens
    assert not (store.caminho.parent / "bling_tokens.tmp").exists()


def test_arquivo_corrompido_levanta_erro_sem_apagar(tmp_path):
    caminho = tmp_path / "bling_tokens.json"
    caminho.write_text("{quebrado", encoding="utf-8")
    with pytest.raises(ErroAutenticacao):
        TokenStore(caminho).carregar()
    assert caminho.read_text(encoding="utf-8") == "{quebrado"


def test_url_autorizacao_contem_client_id_e_state(credenciais, store):
    url = BlingAuth(store).url_autorizacao(state="abc")
    assert "response_type=code" in url
    assert "client_id=cid" in url
    assert "state=abc" in url


def test_url_autorizacao_sem_credenciais_orienta_operador(monkeypatch, store):
    monkeypatch.delenv("BLING_CLIENT_ID", raising=False)
    with pytest.raises(ErroAutenticacao, match="BLING_CLIENT_ID"):
        BlingAuth(store).url_autorizacao()


def test_trocar_codigo_persiste_tokens_normalizados(credenciais, store):
    sessao = Sessao([Resp(200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "token_type": "Bearer", "scope": "geral"})])
    auth = BlingAuth(store, session=sessao, relogio=lambda: 1000.0)
    tokens = auth.trocar_codigo("codigo-123")
    assert sessao.chamadas[0]["data"] == {"grant_type": "authorization_code", "code": "codigo-123"}
    assert tokens["expira_em"] == 4600
    assert store.carregar()["refresh_token"] == "r"
    assert tokens["escopo"] == "geral"


def test_access_token_renova_quando_falta_menos_de_60s(credenciais, store):
    store.salvar({"access_token": "velho", "refresh_token": "r1", "expira_em": 1030})
    sessao = Sessao([Resp(200, {"access_token": "novo", "refresh_token": "r2", "expires_in": 3600})])
    auth = BlingAuth(store, session=sessao, relogio=lambda: 1000.0)
    assert auth.access_token() == "novo"
    assert sessao.chamadas[0]["data"]["grant_type"] == "refresh_token"
    assert sessao.chamadas[0]["data"]["refresh_token"] == "r1"
    assert store.carregar()["refresh_token"] == "r2"


def test_refresh_recusado_preserva_os_tokens_anteriores(credenciais, store):
    store.salvar({"access_token": "velho", "refresh_token": "r1", "expira_em": 0})
    auth = BlingAuth(store, session=Sessao([Resp(400, {"error": "invalid_grant"})]), relogio=lambda: 1000.0)
    with pytest.raises(ErroAutenticacao) as exc:
        auth.access_token()
    assert "velho" not in str(exc.value) and "r1" not in str(exc.value)
    assert store.carregar()["refresh_token"] == "r1"


def test_sem_tokens_orienta_a_rodar_bling_auth(credenciais, store):
    with pytest.raises(ErroAutenticacao, match="bling-auth"):
        BlingAuth(store).access_token()


def test_access_token_usa_semente_do_env_quando_o_arquivo_nao_existe(credenciais, store, monkeypatch):
    monkeypatch.setenv("BLING_ACCESS_TOKEN", "semente")
    auth = BlingAuth(store, relogio=lambda: 1000.0)
    assert auth.access_token() == "semente"
    assert store.carregar() is None  # semear não grava arquivo
