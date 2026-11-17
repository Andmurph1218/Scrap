"""Adapter do provider Nuvemshop sobre o cliente existente (`taxonomia/nuvemshop_api.py`).

Composição, não herança: espelha os caminhos de escrita validados em produção
(`taxonomia/aplicar.py`) — arquivamento com `visibility="hidden"` e queda para
`published=False` em 422, rename de produto com GET antes do PUT para preservar
`es`/`en` — e traduz `ErroNuvemshop` para os erros canônicos de `base.py`.
O fluxo atual de `taxonomia/` não muda.
"""

from __future__ import annotations

from typing import Any

from wago.marketplaces.base import (
    AlteracaoCategoria, AlteracaoProduto, BaseMarketplaceProvider, Categoria, ErroAutenticacao,
    ErroMarketplace, ErroRateLimit, Id, NaoEncontrado, NovaCategoria, Produto,
)
from wago.taxonomia.nuvemshop_api import ErroNuvemshop, NuvemshopAPI, texto_i18n


def traduzir_erro(exc: ErroNuvemshop) -> ErroMarketplace:
    """Mapeia o status HTTP da Nuvemshop para o erro canônico, preservando status/mensagem/corpo."""
    classes = {401: ErroAutenticacao, 403: ErroAutenticacao, 404: NaoEncontrado, 429: ErroRateLimit}
    return classes.get(exc.status, ErroMarketplace)(exc.status, str(exc), exc.corpo)


def _publicado(prod: dict) -> bool | None:
    """Disponível na vitrine: `visibility` vence; sem ele, `published` é fallback legado."""
    if prod.get("visibility"):
        return prod["visibility"] != "hidden"
    if "published" in prod:
        return bool(prod["published"])
    return None


def _sku(prod: dict) -> str:
    """Primeira variante com `sku` preenchido, sem espaços nas bordas."""
    for variante in prod.get("variants") or []:
        if isinstance(variante, dict) and str(variante.get("sku") or "").strip():
            return str(variante["sku"]).strip()
    return ""


def _categoria_canonica(cat: dict) -> Categoria:
    return {"id": cat["id"], "nome": texto_i18n(cat.get("name")), "pai_id": cat.get("parent") or None,
            "descricao": texto_i18n(cat.get("description"))}


def _produto_canonico(prod: dict) -> Produto:
    return {"id": prod["id"], "nome": texto_i18n(prod.get("name")), "sku": _sku(prod),
            "categorias": [c["id"] if isinstance(c, dict) else c for c in prod.get("categories") or []],
            "publicado": _publicado(prod)}


class NuvemshopProvider(BaseMarketplaceProvider):
    """`BaseMarketplaceProvider` que repassa ao `NuvemshopAPI` existente."""

    nome = "nuvemshop"

    def __init__(self, api: NuvemshopAPI | None = None) -> None:
        self.api = api or NuvemshopAPI.do_env()

    # ---- categorias -----------------------------------------------------
    def listar_categorias(self) -> list[Categoria]:
        try:
            brutas = self.api.listar_categorias()
        except ErroNuvemshop as exc:
            raise traduzir_erro(exc) from exc
        return [_categoria_canonica(c) for c in brutas]

    def criar_categoria(self, dados: NovaCategoria) -> Categoria:
        kwargs: dict[str, Any] = {}
        if dados.get("descricao"):  # vazio = não enviar (a API recusa description vazia)
            kwargs["description"] = {"pt": dados["descricao"]}
        try:
            cat, reutilizada = self.api.criar_categoria(dados["nome"], dados.get("pai_id"), **kwargs)
        except ErroNuvemshop as exc:
            raise traduzir_erro(exc) from exc
        return {**_categoria_canonica(cat), "reutilizada": reutilizada}

    def atualizar_categoria(self, id: Id, dados: AlteracaoCategoria) -> Categoria:
        kwargs: dict[str, Any] = {}
        if "nome" in dados:
            kwargs["name"] = {"pt": dados["nome"]}
        if "pai_id" in dados:
            kwargs["parent"] = dados["pai_id"]
        if dados.get("descricao"):
            kwargs["description"] = {"pt": dados["descricao"]}
        try:
            cat = self.api.atualizar_categoria(id, **kwargs)
            if not cat.get("name"):  # resposta do PUT pode vir sem o nome: confere por GET
                cat = self.api.obter_categoria(id)
        except ErroNuvemshop as exc:
            raise traduzir_erro(exc) from exc
        return _categoria_canonica(cat)

    def deletar_categoria(self, id: Id) -> None:
        try:
            self.api.excluir_categoria(id)  # 404 já é idempotente no cliente
        except ErroNuvemshop as exc:
            if exc.status == 404:
                return
            raise traduzir_erro(exc) from exc

    # ---- produtos -------------------------------------------------------
    def listar_produtos(self) -> list[Produto]:
        try:
            brutos = self.api.listar_produtos()
        except ErroNuvemshop as exc:
            raise traduzir_erro(exc) from exc
        return [_produto_canonico(p) for p in brutos]

    def atualizar_produto(self, id: Id, dados: AlteracaoProduto) -> Produto:
        if "sku" in dados:
            raise ErroMarketplace(400, "sku não é atualizável no nível do produto")
        kwargs: dict[str, Any] = {}
        if "categorias" in dados:
            kwargs["categories"] = list(dados["categorias"])
        try:
            if "nome" in dados:  # GET antes do PUT: mescla o name i18n para não apagar es/en
                atual = self.api.obter_produto(id)
                nome_atual = atual.get("name")
                kwargs["name"] = {**nome_atual, "pt": dados["nome"]} if isinstance(nome_atual, dict) \
                    else {"pt": dados["nome"]}
            resultado = self._atualizar_publicacao(id, dados, kwargs)
            if not isinstance(resultado, dict) or not resultado:
                resultado = self.api.obter_produto(id)  # resposta do PUT pode vir vazia
            elif dados.get("publicado") is False and resultado.get("published") is not False:
                resultado = self.api.obter_produto(id)  # resposta do PUT pode vir defasada
        except ErroNuvemshop as exc:
            raise traduzir_erro(exc) from exc
        if dados.get("publicado") is False and isinstance(resultado, dict) and resultado.get("published"):
            raise ErroMarketplace(409, "produto continua publicado")
        return _produto_canonico(resultado)

    def _atualizar_publicacao(self, id: Id, dados: AlteracaoProduto, kwargs: dict[str, Any]) -> Any:
        """Mesmo caminho de `_executar`/`arquivar_produto`: visibility primeiro, published em 422."""
        if "publicado" not in dados:
            return self.api.atualizar_produto(id, **kwargs)
        if dados["publicado"]:
            return self.api.atualizar_produto(id, **kwargs, visibility="visible")
        try:
            return self.api.atualizar_produto(id, **kwargs, visibility="hidden")
        except ErroNuvemshop as exc:
            if exc.status != 422:
                raise
            return self.api.atualizar_produto(id, **kwargs, published=False)
