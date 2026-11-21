"""OAuth 2.0 do Bling e armazenamento local dos tokens.

`TokenStore` grava os tokens em `data/state/bling_tokens.json` de forma atômica
(`.tmp` + `os.replace`, `chmod 0600` best-effort). `BlingAuth` cobre o
`authorization_code`, o `refresh_token` rotativo e a semeadura opcional por
`.env` (`BLING_ACCESS_TOKEN`/`BLING_REFRESH_TOKEN`, nunca persistida).

Falha de refresh não altera o arquivo: o operador refaz o login. Nenhuma
mensagem de erro inclui access/refresh token.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import webbrowser
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from wago.config import env
from wago.marketplaces.base import ErroAutenticacao


#: Porta para callback OAuth local (compatível com Nuvemshop)
OAUTH_PORT = 8791


def autenticar_bling_auto(*, timeout_s: int = 300, abrir_navegador: bool = True) -> dict:
    """Executa o fluxo OAuth semi-automático do Bling.
    
    Sobe listener em 127.0.0.1:{OAUTH_PORT}/callback, abre a URL de autorização
    no navegador, captura o `code`, troca por tokens e salva em data/state/bling_tokens.json.
    """
    client_id = env("BLING_CLIENT_ID") or "c81c1391a291bbf603b61363e8c577be578407fe"
    
    # Gera um state aleatório para validação
    state = secrets.token_hex(8)
    redirect_uri = f"http://127.0.0.1:{OAUTH_PORT}/callback"
    
    auth_url = (
        f"https://www.bling.com.br/Api/v3/oauth/authorize"
        f"?response_type=code&client_id={client_id}&state={state}&redirect_uri={redirect_uri}"
    )
    
    resultado: dict[str, str | None] = {}
    
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            if "code" in qs or "error" in qs:
                # Valida o state
                received_state = (qs.get("state") or [None])[0]
                if received_state != state:
                    resultado["error"] = "state parameter mismatch"
                else:
                    resultado["code"] = (qs.get("code") or [None])[0]
                    resultado["error"] = (qs.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if resultado.get("code"):
                msg = "Autorização Bling capturada com sucesso! Pode fechar esta aba."
            elif resultado.get("error"):
                msg = f"Erro na autorização: {resultado['error']}"
            else:
                msg = "Aguardando autorização no Bling..."
            self.wfile.write(
                f'<h2 style="font-family:sans-serif">{msg}</h2>'.encode("utf-8")
            )

        def log_message(self, *args: Any) -> None:
            return

    print(f"Iniciando autorização OAuth Bling em {redirect_uri} (App {client_id})...")
    try:
        server = HTTPServer(("127.0.0.1", OAUTH_PORT), CallbackHandler)
    except OSError as exc:
        raise SystemExit(
            f"Não foi possível escutar em 127.0.0.1:{OAUTH_PORT} para o callback OAuth: {exc}"
        ) from exc

    server.timeout = 1.0
    try:
        print(f"Abrindo autorização Bling no navegador: {auth_url}")
        if abrir_navegador:
            webbrowser.open(auth_url)
        limite = time.monotonic() + timeout_s
        while (
            "code" not in resultado
            and "error" not in resultado
            and time.monotonic() < limite
        ):
            server.handle_request()
    finally:
        server.server_close()

    code = resultado.get("code")
    if not code:
        motivo = resultado.get("error") or "tempo esgotado aguardando aceite no navegador"
        raise SystemExit(f"Falha na autorização Bling: {motivo}")

    print("Código OAuth recebido. Trocando por Access Token permanente...")
    tokens = BlingAuth().trocar_codigo(code)
    validade = datetime.fromtimestamp(tokens["expira_em"]).isoformat(timespec="seconds")
    print(f"Token do Bling salvo; válido até {validade}")
    return tokens

URL_AUTORIZACAO = "https://www.bling.com.br/Api/v3/oauth/authorize"
URL_TOKEN = "https://www.bling.com.br/Api/v3/oauth/token"

MARGEM_EXPIRACAO = 60  # renova quando faltar menos de 1 minuto
MSG_SEM_TOKENS = "sem tokens do Bling: rode `wago bling-auth --code <code>`"
MSG_SEM_REFRESH = "refresh_token do Bling ausente: rode `wago bling-auth --code <code>`"


def _mensagem_do_corpo(corpo: Any, status: int) -> str:
    """Mensagem curta a partir do corpo do OAuth; nunca contém tokens."""
    if isinstance(corpo, dict):
        partes = [str(corpo.get(chave) or "").strip() for chave in ("error", "error_description", "message")]
        texto = " - ".join(parte for parte in partes if parte)
        if texto:
            return texto
    return f"falha de OAuth do Bling (HTTP {status})"


def _mascarar(texto: str, *segredos: Any) -> str:
    """Remove segredos conhecidos da mensagem, caso a API os ecoe no corpo."""
    for segredo in segredos:
        valor = str(segredo or "")
        if len(valor) >= 4:
            texto = texto.replace(valor, "***")
    return texto


def _corpo_sem_segredos(corpo: Any) -> Any:
    """Cópia do corpo com chaves sensíveis redigidas antes de anexar à exceção.

    A mensagem já é mascarada, mas o corpo bruto iria junto em `.corpo` e um
    handler que o logue vazaria tokens. Chaves com `token`/`secret` viram
    `"***"`, recursivamente.
    """
    if isinstance(corpo, dict):
        return {
            chave: "***" if any(p in str(chave).lower() for p in ("token", "secret")) else _corpo_sem_segredos(valor)
            for chave, valor in corpo.items()
        }
    if isinstance(corpo, list):
        return [_corpo_sem_segredos(item) for item in corpo]
    return corpo


def _corpo_json(resposta: Any) -> Any:
    try:
        return resposta.json()
    except ValueError:
        return None


class TokenStore:
    """Leitura/escrita do arquivo de tokens do Bling."""

    def __init__(self, caminho: Path | None = None) -> None:
        from wago.config import STATE  # import local: permite monkeypatch de wago.config.STATE

        self.caminho = Path(caminho) if caminho else STATE / "bling_tokens.json"

    def carregar(self) -> dict | None:
        """Tokens salvos ou `None` quando o arquivo não existe.

        Arquivo ilegível/corrompido vira `ErroAutenticacao` sem tocar no arquivo,
        para o operador inspecionar e refazer o login.
        """
        try:
            bruto = self.caminho.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise ErroAutenticacao(401, f"arquivo de tokens do Bling ilegível: {self.caminho}") from exc
        try:
            tokens = json.loads(bruto)
        except json.JSONDecodeError as exc:
            raise ErroAutenticacao(401, f"arquivo de tokens do Bling ilegível: {self.caminho}") from exc
        if not isinstance(tokens, dict):
            raise ErroAutenticacao(401, f"arquivo de tokens do Bling ilegível: {self.caminho}")
        return tokens

    def salvar(self, tokens: dict) -> None:
        """Grava os tokens atomicamente, com `atualizado_em` e permissão restrita."""
        dados = dict(tokens)
        if not str(dados.get("access_token") or "").strip():
            raise ErroAutenticacao(401, "tokens do Bling inválidos: access_token ausente")
        if not str(dados.get("refresh_token") or "").strip():
            raise ErroAutenticacao(401, "tokens do Bling inválidos: refresh_token ausente")
        dados["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
        temporario = self.caminho.with_suffix(".tmp")
        try:
            self.caminho.parent.mkdir(parents=True, exist_ok=True)
            temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporario, self.caminho)
        except OSError as exc:
            raise ErroAutenticacao(401, f"não foi possível gravar os tokens do Bling em {self.caminho}") from exc
        finally:
            try:
                temporario.unlink(missing_ok=True)  # sem sobra de .tmp, mesmo em falha
            except OSError:
                pass
        try:
            os.chmod(self.caminho, 0o600)
        except OSError:
            pass


class BlingAuth:
    """Authorization_code, refresh rotativo e acesso ao token válido."""

    def __init__(
        self,
        store: TokenStore | None = None,
        *,
        session: Any = None,
        relogio: Callable[[], float] = time.time,
    ) -> None:
        self.store = TokenStore() if store is None else store
        self.session = requests if session is None else session
        self.relogio = relogio

    # ---- OAuth ----------------------------------------------------------
    def url_autorizacao(self, state: str | None = None) -> str:
        """URL de consentimento; `state` é repassado quando informado."""
        client_id = env("BLING_CLIENT_ID")
        if not client_id:
            raise ErroAutenticacao(401, "BLING_CLIENT_ID ausente no .env")
        parametros = {"response_type": "code", "client_id": client_id}
        # Inclui um state aleatório se não for informado, para satisfazer apps que exigem validação
        if not state:
            import secrets
            state = secrets.token_hex(8)
        parametros["state"] = state
        redirect_uri = env("BLING_REDIRECT_URI")
        if redirect_uri:
            parametros["redirect_uri"] = redirect_uri
        return f"{URL_AUTORIZACAO}?{urlencode(parametros)}"

    def trocar_codigo(self, code: str) -> dict:
        """Troca o `code` do consentimento por tokens e persiste o resultado."""
        dados = {"grant_type": "authorization_code", "code": code}
        redirect_uri = env("BLING_REDIRECT_URI")
        if redirect_uri:
            dados["redirect_uri"] = redirect_uri
        tokens = self._normalizar(self._pedir_token(dados))
        self.store.salvar(tokens)
        return tokens

    def renovar(self) -> dict:
        """Renova via `refresh_token` e persiste; falha preserva o arquivo anterior."""
        tokens = self.store.carregar() or self._tokens_do_env()
        if not tokens:
            raise ErroAutenticacao(401, MSG_SEM_TOKENS)
        refresh = str(tokens.get("refresh_token") or "").strip()
        if not refresh:
            raise ErroAutenticacao(401, MSG_SEM_REFRESH)
        novos = self._normalizar(self._pedir_token({"grant_type": "refresh_token", "refresh_token": refresh}))
        if not novos["refresh_token"]:  # rotação só quando o Bling manda um novo
            novos["refresh_token"] = refresh
        self.store.salvar(novos)
        return novos

    def access_token(self) -> str:
        """Token utilizável agora, renovando quando faltar menos de 60s."""
        tokens = self.store.carregar() or self._tokens_do_env()
        if not tokens:
            raise ErroAutenticacao(401, MSG_SEM_TOKENS)
        if self._precisa_renovar(tokens):
            if not str(tokens.get("refresh_token") or "").strip():
                raise ErroAutenticacao(401, MSG_SEM_REFRESH)
            tokens = self.renovar()
        access = str(tokens.get("access_token") or "").strip()
        if not access:
            raise ErroAutenticacao(401, MSG_SEM_TOKENS)
        return access

    # ---- internos -------------------------------------------------------
    def _precisa_renovar(self, tokens: dict) -> bool:
        try:
            return float(tokens.get("expira_em")) - self.relogio() < MARGEM_EXPIRACAO
        except (TypeError, ValueError):
            return True

    def _tokens_do_env(self) -> dict | None:
        """Semente do `.env`; usada só quando não há arquivo e nunca persistida."""
        access = env("BLING_ACCESS_TOKEN")
        refresh = env("BLING_REFRESH_TOKEN")
        if not access and not refresh:
            return None
        return {
            "access_token": access or "",
            "refresh_token": refresh or "",
            "expira_em": int(self.relogio()) + 3600 if access else 0,
            "token_type": "Bearer",
            "escopo": "",
            "atualizado_em": "",
        }

    def _credenciais(self) -> tuple[str, str]:
        client_id = env("BLING_CLIENT_ID")
        client_secret = env("BLING_CLIENT_SECRET")
        ausentes = [
            nome
            for nome, valor in (("BLING_CLIENT_ID", client_id), ("BLING_CLIENT_SECRET", client_secret))
            if not valor
        ]
        if ausentes:
            raise ErroAutenticacao(401, f"{', '.join(ausentes)} ausente(s) no .env")
        return str(client_id), str(client_secret)

    def _pedir_token(self, dados: dict) -> dict:
        """POST no endpoint de token com Basic auth; traduz falhas para `ErroAutenticacao`."""
        client_id, client_secret = self._credenciais()
        try:
            resposta = self.session.post(URL_TOKEN, data=dados, auth=(client_id, client_secret), timeout=30)
        except requests.RequestException as exc:
            raise ErroAutenticacao(401, f"falha de rede no OAuth do Bling: {exc}") from exc
        corpo = _corpo_json(resposta)
        if resposta.status_code >= 400:
            segredos = [dados.get("refresh_token"), dados.get("code"), client_secret]
            if isinstance(corpo, dict):
                segredos += [corpo.get("access_token"), corpo.get("refresh_token")]
            mensagem = _mascarar(_mensagem_do_corpo(corpo, resposta.status_code), *segredos)
            raise ErroAutenticacao(resposta.status_code, mensagem, _corpo_sem_segredos(corpo))
        if not isinstance(corpo, dict):
            raise ErroAutenticacao(401, "resposta de token do Bling inválida", corpo)
        return corpo

    def _normalizar(self, resposta: dict) -> dict:
        """Aceita `{"data": {...}}` ou o objeto direto e devolve os campos persistidos."""
        dados = resposta.get("data") if isinstance(resposta, dict) else None
        if not isinstance(dados, dict):
            dados = resposta if isinstance(resposta, dict) else {}
        try:
            expires_in = int(dados.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        tokens = {
            "access_token": str(dados.get("access_token") or ""),
            "refresh_token": str(dados.get("refresh_token") or ""),
            "expira_em": int(self.relogio() + expires_in),
            "token_type": dados.get("token_type") or "Bearer",
            "escopo": dados.get("scope") or "",
            "atualizado_em": datetime.now().isoformat(timespec="seconds"),
        }
        if not tokens["access_token"]:
            raise ErroAutenticacao(401, "resposta de token do Bling sem access_token")
        return tokens
