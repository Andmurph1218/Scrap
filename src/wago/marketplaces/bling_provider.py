"""Provider do Bling v3: cliente HTTP e mapeamento canônico no mesmo módulo.

`BlingClient` concentra as chamadas HTTP, o envelope `{"data": ...}`, a
paginação por `pagina`/`limite` e o retry de 401 (renovação de token), 429
(limite de requisições) e 5xx (instabilidade). `BlingProvider` traduz o
contrato de `base.py` para os caminhos e campos do Bling.

Nenhuma mensagem de erro inclui access token ou refresh token.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import requests

from wago.marketplaces.base import (
    AlteracaoCategoria,
    AlteracaoProduto,
    BaseMarketplaceProvider,
    Categoria,
    CategoriaComProdutos,
    ErroAutenticacao,
    ErroMarketplace,
    ErroRateLimit,
    Id,
    NaoEncontrado,
    NovaCategoria,
    Produto,
)
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

#: Campos de produto que só o Bling escreve: ficam fora do corpo do PUT.
CAMPOS_PRODUTO_SO_LEITURA = frozenset({"id", "dataCriacao", "dataAlteracao", "dataExclusao"})


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


def _dicionario(corpo: Any) -> dict:
    """Corpo desembrulhado como dict; `None` e formatos inesperados viram `{}`."""
    desembrulhado = _desembrulhar(corpo)
    return desembrulhado if isinstance(desembrulhado, dict) else {}


def _id_categoria(bruto: Any) -> Id | None:
    """Id de uma categoria bruta (dict com `id` ou o próprio id)."""
    return bruto.get("id") if isinstance(bruto, dict) else bruto


def _ids_produto(prod: dict) -> list[Id]:
    """Ids de categoria de um produto bruto: `categoria` primeiro, depois `categorias`.

    Aceita dicts (`{"id": ...}`) ou ids diretos, preserva a ordem e remove
    duplicados.
    """
    brutos = [prod.get("categoria"), *(prod.get("categorias") or [])]
    ids: list[Id] = []
    for bruto in brutos:
        valor = _id_categoria(bruto)
        if valor is not None and valor not in ids:
            ids.append(valor)
    return ids


def _categoria_canonica(cat: dict) -> Categoria:
    """Categoria do Bling no contrato canônico; `descricao` nunca é enviada de volta."""
    return {
        "id": cat.get("id"),
        "nome": str(cat.get("descricao") or ""),
        "pai_id": cat.get("categoriaPaiId") or None,
        "descricao": "",
    }


def _produto_canonico(prod: dict) -> Produto:
    """Produto do Bling no contrato canônico; `publicado` não tem equivalente."""
    return {
        "id": prod.get("id"),
        "nome": str(prod.get("nome") or ""),
        "sku": str(prod.get("codigo") or ""),
        "categorias": _ids_produto(prod),
        "publicado": None,
    }


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


class BlingProvider(BaseMarketplaceProvider):
    """`BaseMarketplaceProvider` que mapeia o contrato canônico para o Bling v3.

    No Bling a `descricao` da categoria **é** o nome; por isso a leitura devolve
    `descricao` vazia e o envio de `descricao` de categoria levanta erro em vez
    de renomear por acidente. `publicado` não tem equivalente confiável e
    também é recusado na escrita.
    """

    nome = "bling"

    def __init__(self, client: BlingClient) -> None:
        self.client = client

    # ---- categorias -----------------------------------------------------
    def listar_categorias(self) -> list[Categoria]:
        return [_categoria_canonica(c) for c in self.client.paginar("/categorias/produtos")]

    def criar_categoria(self, dados: NovaCategoria) -> Categoria:
        nome = dados["nome"]
        descricao = dados.get("descricao")
        if descricao and descricao != nome:
            raise ErroMarketplace(400, "Bling não separa descrição do nome da categoria")
        corpo: dict[str, Any] = {"descricao": nome}
        if dados.get("pai_id") is not None:
            corpo["categoriaPaiId"] = dados["pai_id"]
        resposta = self.client.requisitar("POST", "/categorias/produtos", json=corpo)
        return {**_categoria_canonica({**corpo, **_dicionario(resposta)}), "reutilizada": False}

    def atualizar_categoria(self, id: Id, dados: AlteracaoCategoria) -> Categoria:
        if "descricao" in dados:
            raise ErroMarketplace(400, "descricao de categoria não é suportada no Bling: use nome")
        corpo: dict[str, Any] = {}
        if "nome" in dados:
            corpo["descricao"] = dados["nome"]
        if "pai_id" in dados:
            corpo["categoriaPaiId"] = dados["pai_id"]
        if not corpo:
            raise ErroMarketplace(400, "nenhum campo para atualizar")
        resposta = self.client.requisitar("PUT", f"/categorias/produtos/{id}", json=corpo)
        return _categoria_canonica({"id": id, **corpo, **_dicionario(resposta)})

    def deletar_categoria(self, id: Id) -> None:
        try:
            resposta = self.client.requisitar(
                "GET", "/produtos", params={"idCategoria": id, "pagina": 1, "limite": 1}
            )
        except NaoEncontrado:
            resposta = None
        except ErroMarketplace as exc:
            if exc.status != 400:
                raise
            resposta = None  # filtro recusado: só o DELETE pode dizer se há vínculo
        if resposta is not None and _lista(resposta):
            raise CategoriaComProdutos(409, f"categoria {id} tem produtos vinculados", resposta)
        try:
            self.client.requisitar("DELETE", f"/categorias/produtos/{id}")
        except NaoEncontrado:
            return  # já não existe: idempotente
        except ErroMarketplace as exc:
            if exc.status != 400:
                raise
            raise CategoriaComProdutos(409, "Bling recusou a exclusão: categoria com produtos", exc.corpo) from exc

    # ---- produtos -------------------------------------------------------
    def listar_produtos(self) -> list[Produto]:
        return [_produto_canonico(p) for p in self.client.paginar("/produtos")]

    def atualizar_produto(self, id: Id, dados: AlteracaoProduto) -> Produto:
        if "publicado" in dados:
            raise ErroMarketplace(400, "publicado não é suportado no Bling")
        atual = _dicionario(self.client.requisitar("GET", f"/produtos/{id}"))
        # O PUT sobrescreve o produto: parte do atual para não apagar campos omitidos.
        corpo = {k: v for k, v in atual.items() if k not in CAMPOS_PRODUTO_SO_LEITURA}
        if "nome" in dados:
            corpo["nome"] = dados["nome"]
        if "categorias" in dados:
            ids = list(dados["categorias"])
            corpo.pop("categoria", None)
            corpo.pop("categorias", None)
            if len(ids) == 1:
                corpo["categoria"] = {"id": ids[0]}
            else:
                corpo["categorias"] = [{"id": cat} for cat in ids]
        resposta = self.client.requisitar("PUT", f"/produtos/{id}", json=corpo)
        return _produto_canonico({**atual, **corpo, **_dicionario(resposta)})
