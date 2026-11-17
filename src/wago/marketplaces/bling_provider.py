"""Camada HTTP do Bling v3: cliente com paginação, rate limit e retry.

`BlingClient` concentra as chamadas HTTP, o envelope `{"data": ...}`, a
paginação por `pagina`/`limite` e o retry de 401 (renovação de token), 429
(limite de requisições) e 5xx (instabilidade). O mapeamento canônico fica em
`BlingProvider` (mesmo módulo), adicionado na etapa seguinte.

Nenhuma mensagem de erro inclui access token ou refresh token.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import requests

from wago.marketplaces.base import ErroAutenticacao, ErroMarketplace, ErroRateLimit, NaoEncontrado
from wago.marketplaces.bling_auth import BlingAuth
from wago.marketplaces.rate_limit import RateLimiter

#: Tentativas totais de uma requisição antes de desistir (429 e 5xx).
MAX_TENTATIVAS = 5

#: Tamanho de página usado na paginação (padrão da API do Bling).
LIMITE_PAGINA = 100

#: Base da API v3 do Bling.
URL_BASE = "https://www.bling.com.br/Api/v3"

MSG_LIMITE_ESGOTADO = "limite de requisições do Bling esgotado"
MSG_INSTABILIDADE = "instabilidade do Bling"


def _desembrulhar(corpo: Any) -> Any:
    """Aceita `{"data": ...}` ou o objeto direto, devolvendo o conteúdo útil."""
    if isinstance(corpo, dict) and "data" in corpo:
        return corpo["data"]
    return corpo


def _lista(corpo: Any) -> list:
    """Conteúdo do corpo como lista; `data: null` e formatos inesperados viram `[]`."""
    desembrulhado = _desembrulhar(corpo)
    return desembrulhado if isinstance(desembrulhado, list) else []


def _corpo_json(resposta: Any) -> Any:
    """JSON do corpo da resposta; `None` sem corpo e texto puro quando não é JSON."""
    if not resposta.content:
        return None
    try:
        return resposta.json()
    except ValueError:
        return resposta.text


class BlingClient:
    """Requisições autenticadas ao Bling v3, com paginação e retry."""

    #: Mesmo helper exposto na classe para uso dos mapeamentos.
    _desembrulhar = staticmethod(_desembrulhar)

    def __init__(
        self,
        auth: BlingAuth,
        *,
        session: Any = None,
        limiter: RateLimiter | None = None,
        timeout: int = 30,
        url_base: str = URL_BASE,
    ) -> None:
        self.auth = auth
        self.session = requests if session is None else session
        self.limiter = limiter or RateLimiter()
        self.timeout = timeout
        self.url_base = url_base.rstrip("/")

    # ---- HTTP -----------------------------------------------------------
    def requisitar(
        self,
        metodo: str,
        caminho: str,
        *,
        params: dict | None = None,
        json: Any = None,
    ) -> Any:
        """Executa a requisição e devolve o corpo desembrulhado.

        O rate limiter é consultado antes de cada tentativa, inclusive nas
        repetições. Um 401 renova o token uma única vez e repete sem consumir
        tentativa; um segundo 401 vira `ErroAutenticacao`.
        """
        url = f"{self.url_base}{caminho}"
        renovou = False
        tentativa = 1
        while tentativa <= MAX_TENTATIVAS:
            self.limiter.adquirir()
            resposta = self.session.request(
                metodo,
                url,
                headers=self._headers(),
                params=params,
                json=json,
                timeout=self.timeout,
            )
            status = resposta.status_code

            if status == 401 and not renovou:
                # Token expirado: renova e repete sem gastar tentativa.
                self.auth.renovar()
                renovou = True
                continue

            if status >= 400:
                corpo = _corpo_json(resposta)
                if status == 429:
                    if tentativa >= MAX_TENTATIVAS:
                        raise ErroRateLimit(429, MSG_LIMITE_ESGOTADO, corpo)
                    self.limiter.aguardar_429(resposta.headers, tentativa)
                    tentativa += 1
                    continue
                if 500 <= status < 600:
                    if tentativa >= MAX_TENTATIVAS:
                        raise ErroMarketplace(status, MSG_INSTABILIDADE, corpo)
                    self.limiter.aguardar_429(resposta.headers, tentativa)
                    tentativa += 1
                    continue
                raise self._erro(status, corpo)

            return _desembrulhar(_corpo_json(resposta))

        # Inalcançável: o laço só sai por retorno ou exceção.
        raise ErroMarketplace(500, MSG_INSTABILIDADE)

    def paginar(self, caminho: str, params: dict | None = None) -> Iterator[dict]:
        """Percorre todas as páginas, parando em página vazia ou incompleta."""
        pagina = 1
        while True:
            itens = _lista(
                self.requisitar(
                    "GET",
                    caminho,
                    params={**(params or {}), "pagina": pagina, "limite": LIMITE_PAGINA},
                )
            )
            if not itens:
                return
            yield from itens
            if len(itens) < LIMITE_PAGINA:
                return
            pagina += 1

    # ---- erros ----------------------------------------------------------
    @staticmethod
    def _erro(status: int, corpo: Any) -> ErroMarketplace:
        """Traduz status/corpo do Bling para o erro canônico correspondente."""
        mensagem = BlingClient._mensagem(corpo)
        if status in (401, 403):
            return ErroAutenticacao(status, mensagem, corpo)
        if status == 404:
            return NaoEncontrado(status, mensagem, corpo)
        if status == 429:
            return ErroRateLimit(status, mensagem, corpo)
        return ErroMarketplace(status, mensagem, corpo)

    @staticmethod
    def _mensagem(corpo: Any) -> str:
        """Mensagem do corpo na ordem de campos da API do Bling."""
        if isinstance(corpo, dict):
            erro = corpo.get("error")
            if isinstance(erro, dict):
                for chave in ("message", "description"):
                    valor = erro.get(chave)
                    if valor:
                        return str(valor)
            elif isinstance(erro, str) and erro.strip():
                return erro
            mensagem = corpo.get("message")
            if mensagem:
                return str(mensagem)
        return str(corpo)

    # ---- headers --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        """Headers de toda requisição, com o token válido no momento da chamada."""
        return {
            "Authorization": f"Bearer {self.auth.access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
