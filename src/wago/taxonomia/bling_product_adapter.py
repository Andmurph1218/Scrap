"""Adapter para converter produtos do Bling v3 para o formato canônico esperado pelo classificador.

O Bling usa:
- `nome` (string): nome do produto
- `descricao` (string): descrição detalhada  
- `situacao` (string): status do produto ('Ativo', 'Inativo', 'Excluido')
- `categorias` (array): lista de categorias com campo `id`

O classificador espera:
- `id`: ID do produto
- `nome`: nome do produto
- `descricao_html`: descrição (HTML)
- `categorias`: lista de IDs de categorias
- `skus`: lista de SKUs
- `publicado`: boolean (visibilidade)
"""

from __future__ import annotations

from typing import Any

from wago.taxonomia.texto import sanitizar_texto


# Mapeamento de situação Bling → publicado
_SITUACAO_PUBLICADO = {
    "Ativo": True,
    "Ativa": True,
    "Inativo": False,
    "Inativa": False,
    "Excluido": False,
    "Excluído": False,
}


def adaptar_produto_bling(prod_bling: dict[str, Any]) -> dict[str, Any]:
    """Converte um produto do Bling para o formato canônico do classificador.
    
    Args:
        prod_bling: Produto no formato Bling v3 API
        
    Returns:
        Produto no formato canônico esperado pelo classificador
        
    Exemplo de input Bling:
        {
            "id": 16738203569,
            "nome": "Tomada ABC",
            "descricao": "<p>Tomada de parafuso...</p>",
            "situacao": "Ativo",
            "categorias": [{"id": 9851416}],
            "sku": "SKU123"
        }
        
    Saída canônica:
        {
            "id": 16738203569,
            "nome": "Tomada ABC",
            "descricao_html": "Tomada de parafuso...",
            "categorias": [9851416],
            "skus": ["SKU123"],
            "publicado": True
        }
    """
    # Extrair ID
    produto_id = prod_bling.get("id")
    if produto_id is None:
        raise ValueError("Produto sem ID")
    
    # Extrair e sanitizar nome
    nome = prod_bling.get("nome") or ""
    
    # Extrair e sanitizar descrição (HTML será processado pelo sanitizador)
    descricao_raw = prod_bling.get("descricao") or prod_bling.get("descricaoHtml") or ""
    descricao_html = sanitizar_texto(str(descricao_raw))
    
    # Extrair categorias como lista de IDs
    categorias = []
    for cat in prod_bling.get("categorias") or []:
        if isinstance(cat, dict):
            if cat_id := cat.get("id"):
                categorias.append(cat_id)
        elif isinstance(cat, (int, str)):
            categorias.append(int(cat) if isinstance(cat, str) and cat.isdigit() else cat)
    
    # Extrair SKUs
    skus = []
    if sku := prod_bling.get("sku"):
        skus = [sku] if isinstance(sku, str) else []
    for s in prod_bling.get("skus") or []:
        if isinstance(s, str) and s:
            skus.append(s)
    
    # Determinar publicado baseado na situação
    situacao = prod_bling.get("situacao") or "Ativo"
    if isinstance(situacao, str):
        situacao = situacao.strip().capitalize()
    publicado = _SITUACAO_PUBLICADO.get(situacao, True)
    
    return {
        "id": int(produto_id),
        "nome": nome,
        "descricao_html": descricao_html,
        "categorias": categorias,
        "skus": skus,
        "publicado": publicado,
        # Manter campos originais para debugging
        "_situacao_original": situacao,
        "_bling_original": True,
    }


def adaptar_produtos_bling(lote: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Converte uma lista de produtos do Bling para o formato canônico.
    
    Args:
        lote: Lista de produtos no formato Bling
        
    Returns:
        Lista de produtos no formato canônico
    """
    return [adaptar_produto_bling(p) for p in lote if p.get("id")]


def validar_categoria_corrente(prod: dict[str, Any], categoria_id: int | None) -> tuple[bool, str | None]:
    """Valida se a categoria atual do produto bate com o ID esperado.
    
    Args:
        prod: Produto adaptado
        categoria_id: ID da categoria esperado (do Bling)
        
    Returns:
        (é válido, mensagem de erro)
    """
    categorias_atuais = prod.get("categorias", [])
    
    if categoria_id is None:
        # Produto sem categoria
        if categorias_atuais:
            return False, f"Produto tem categoria {categorias_atuais}, mas deveria estar sem categoria"
        return True, None
    
    if categoria_id in categorias_atuais:
        return True, None
    
    return False, f"Produto com categoria {categorias_atuais}, esperado {categoria_id}"


def extrair_informacoes_produto(prod_adaptado: dict[str, Any]) -> dict[str, Any]:
    """Extrai informações relevantes de um produto adaptado para logging/debug.
    
    Args:
        prod_adaptado: Produto no formato canônico
        
    Returns:
        Dicionário com informações resumidas
    """
    return {
        "id": prod_adaptado.get("id"),
        "nome": prod_adaptado.get("nome", "")[:50],
        "categorias": prod_adaptado.get("categorias", []),
        "publicado": prod_adaptado.get("publicado"),
        "situacao_original": prod_adaptado.get("_situacao_original"),
    }