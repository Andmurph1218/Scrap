"""Providers de marketplace: contrato canônico, adapter Nuvemshop e provider Bling.

Aqui ficam os exports públicos do pacote e a factory `criar_provider`, que é o
ponto de entrada usado pelos comandos (`wago bling-auth` e `wago bling-categorias`).
Os módulos internos importam `base.py` sempre pelo caminho completo
(`wago.marketplaces.base`), então reexportar não cria ciclo.
"""

from __future__ import annotations

from typing import Any

from wago.marketplaces.base import (
    AlteracaoCategoria,
    AlteracaoProduto,
    BaseMarketplaceProvider,
    Categoria,
    CategoriaComProdutos,
    ErroAutenticacao,
    ErroMarketplace,
    ErroRateLimit,
    NaoEncontrado,
    NovaCategoria,
    Produto,
)
from wago.marketplaces.bling_auth import BlingAuth, TokenStore
from wago.marketplaces.bling_provider import BlingClient, BlingProvider
from wago.marketplaces.nuvemshop_provider import NuvemshopProvider
from wago.marketplaces.rate_limit import RateLimiter

__all__ = [
    "AlteracaoCategoria",
    "AlteracaoProduto",
    "BaseMarketplaceProvider",
    "BlingAuth",
    "BlingClient",
    "BlingProvider",
    "Categoria",
    "CategoriaComProdutos",
    "ErroAutenticacao",
    "ErroMarketplace",
    "ErroRateLimit",
    "NaoEncontrado",
    "NovaCategoria",
    "NuvemshopProvider",
    "Produto",
    "RateLimiter",
    "TokenStore",
    "criar_provider",
]


def criar_provider(nome: str, **kwargs: Any) -> BaseMarketplaceProvider:
    """Instancia o provider pelo nome curto do marketplace.

    `nuvemshop` repassa os kwargs ao `NuvemshopProvider` (ex.: `api=`);
    `bling` aceita um `client=` já construído ou monta
    `BlingClient(BlingAuth(**kwargs))`.
    """
    if nome == "nuvemshop":
        return NuvemshopProvider(**kwargs)
    if nome == "bling":
        client = kwargs.pop("client", None)
        return BlingProvider(client or BlingClient(BlingAuth(**kwargs)))
    raise ValueError(f"marketplace desconhecido: {nome!r}; use 'nuvemshop' ou 'bling'")
