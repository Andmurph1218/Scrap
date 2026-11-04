"""Cliente da Admin API da Nuvemshop para categorias e produtos.

Paginação completa, respeito ao rate limit (leaky bucket) e retry em 429/5xx.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import requests

from wago.config import env, require_env

API_BASE = "https://api.nuvemshop.com.br/v1"
USER_AGENT_PADRAO = "JHSHOPPING (contato@jhshopping.com.br)"
PER_PAGE = 200
CAMPOS_PRODUTO = "id,name,handle,description,categories,variants,published,created_at,updated_at"


class ErroNuvemshop(Exception):
    def __init__(self, status: int, mensagem: str, corpo: Any = None) -> None:
        super().__init__(f"HTTP {status}: {mensagem}")
        self.status = status
        self.corpo = corpo


class Cancelado(Exception):
    pass


def texto_i18n(valor: Any, idioma: str = "pt") -> str:
    """Campos multilíngues da Nuvemshop vêm como {"pt": "...", "es": "..."}."""
    if isinstance(valor, dict):
        for chave in (idioma, "pt", "es", "en"):
            if valor.get(chave):
                return str(valor[chave]).strip()
        for v in valor.values():
            if v:
                return str(v).strip()
        return ""
    return str(valor or "").strip()


class NuvemshopAPI:
    def __init__(
        self,
        store_id: str,
        token: str,
        *,
        user_agent: str = USER_AGENT_PADRAO,
        session: Any = None,
        tentativas: int = 5,
        timeout: int = 30,
        pausa_base: float = 1.0,
        dormir: Callable[[float], None] = time.sleep,
        cancelado: Callable[[], bool] | None = None,
    ) -> None:
        self.store_id = str(store_id)
        self.base = f"{API_BASE}/{self.store_id}"
        self.session = session or requests.Session()
        self.headers = {
            "Authentication": f"bearer {token}",
            "User-Agent": user_agent or USER_AGENT_PADRAO,
            "Content-Type": "application/json",
        }
        self.tentativas = tentativas
        self.timeout = timeout
        self.pausa_base = pausa_base
        self.dormir = dormir
        self.cancelado = cancelado
        self.requisicoes = 0

    @classmethod
    def do_env(cls, **kwargs: Any) -> NuvemshopAPI:
        creds = require_env("NUVEMSHOP_STORE_ID", "NUVEMSHOP_ACCESS_TOKEN")
        return cls(
            creds["NUVEMSHOP_STORE_ID"],
            creds["NUVEMSHOP_ACCESS_TOKEN"],
            user_agent=env("NUVEMSHOP_USER_AGENT", USER_AGENT_PADRAO) or USER_AGENT_PADRAO,
            **kwargs,
        )

    def _checar_cancelamento(self) -> None:
        if self.cancelado and self.cancelado():
            raise Cancelado("Operação cancelada pelo operador.")

    def _respeitar_rate_limit(self, resp: Any) -> None:
        """Header x-rate-limit-remaining baixo → espera o reset (em ms)."""
        try:
            restante = int(resp.headers.get("x-rate-limit-remaining", "99"))
        except (TypeError, ValueError):
            return
        if restante <= 2:
            try:
                reset_ms = int(resp.headers.get("x-rate-limit-reset", "1000"))
            except (TypeError, ValueError):
                reset_ms = 1000
            self.dormir(min(max(reset_ms, 500), 10000) / 1000)

    def requisitar(
        self, metodo: str, caminho: str, *, params: dict | None = None, json: Any = None
    ) -> Any:
        url = f"{self.base}{caminho}"
        ultimo: ErroNuvemshop | None = None
        for tentativa in range(1, self.tentativas + 1):
            self._checar_cancelamento()
            try:
                resp = self.session.request(
                    metodo, url, headers=self.headers, params=params, json=json,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                ultimo = ErroNuvemshop(0, f"falha de rede: {exc.__class__.__name__}")
                self.dormir(self.pausa_base * 2 ** (tentativa - 1))
                continue
            self.requisicoes += 1
            if resp.status_code == 429 or resp.status_code >= 500:
                ultimo = ErroNuvemshop(resp.status_code, "limite/instabilidade da API")
                espera = self.pausa_base * 2 ** (tentativa - 1)
                try:
                    espera = max(espera, int(resp.headers.get("x-rate-limit-reset", "0")) / 1000)
                except (TypeError, ValueError):
                    pass
                self.dormir(min(espera, 30))
                continue
            self._respeitar_rate_limit(resp)
            try:
                corpo = resp.json() if resp.content else None
            except ValueError:
                corpo = resp.text
            if resp.status_code >= 400:
                msg = corpo.get("description") or corpo.get("message") if isinstance(corpo, dict) else str(corpo)
                raise ErroNuvemshop(resp.status_code, str(msg or "erro"), corpo)
            return corpo
        raise ultimo or ErroNuvemshop(0, "falha desconhecida")

    def paginar(self, caminho: str, params: dict | None = None) -> Iterator[dict]:
        pagina = 1
        while True:
            consulta = {**(params or {}), "per_page": PER_PAGE, "page": pagina}
            try:
                itens = self.requisitar("GET", caminho, params=consulta)
            except ErroNuvemshop as exc:
                # Nuvemshop responde 404 "Last page is N" ao passar da última página.
                if exc.status == 404 and pagina > 1:
                    return
                raise
            if not itens:
                return
            yield from itens
            if len(itens) < PER_PAGE:
                return
            pagina += 1

    # ---- leitura -------------------------------------------------------
    def listar_categorias(self) -> list[dict]:
        return list(self.paginar("/categories"))

    def obter_categoria(self, categoria_id: int) -> dict:
        return self.requisitar("GET", f"/categories/{categoria_id}")

    def listar_produtos(
        self, *, campos: str = CAMPOS_PRODUTO, progresso: Callable[[int], None] | None = None,
        **filtros: Any,
    ) -> list[dict]:
        produtos: list[dict] = []
        for prod in self.paginar("/products", {"fields": campos, **filtros}):
            produtos.append(prod)
            if progresso and len(produtos) % PER_PAGE == 0:
                progresso(len(produtos))
        if progresso:
            progresso(len(produtos))
        return produtos

    def produtos_da_categoria(self, categoria_id: int) -> list[dict]:
        return self.listar_produtos(campos="id,categories", category_id=categoria_id)
