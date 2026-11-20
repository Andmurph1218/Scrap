#!/usr/bin/env python3
"""Canary-bling-v2.json - Teste mínimo de atribuição de categoria.

BASEADO NO PROBLEMA IDENTIFICADO:
O Bling v3 PUT /produtos/{id} permite ATtribuir categoria mas NÃO permite remover.

O canary testa:
1. Atribuir categoria a produto sem categoria → categoria é adicionada
2. Arquivar produto ativo → produto fica inativo  
3. Rollback do arquivamento → produto volta a ativo

IMPORTANTE: O rollback DO PRODUTO SEM CATEGORIA nao remove a categoria atribuída.
Esta é uma limitação documentada da API do Bling v3.
"""

import json
from datetime import datetime
from pathlib import Path

canary_path = Path('canary-bling-v2.json')

canary = {
    "tipo": "canary-v2",
    "versao": 3,
    "gerado_em": "2026-11-20T03:20:00",
    "provider": "bling",
    "status": "pendente_execucao",
    "descricao": "Teste de atribuir, arquivar e rollback no Bling v3",
    "documentacao": {
        "limite_api": "O PUT do Bling não permite remover categoria (payload categorias=[] é ignorado)",
        "solucao": "Para remover categoria, usar DELETE ou contato com suporte Bling",
        "produtos_sem_categoria_snapshot": 34031,
        "produtos_com_categoria_snapshot": 820,
    },
    "ambiente": {
        "production": False,
        "nota": "Uso em ambiente de teste - produtos podem ter categoria removida manualmente"
    },
    "acoes": [
        {
            "seq": 1,
            "acao": "atribuir_categoria",
            "status": "pendente",
            "tipo": "atribuir",
            "produto": {
                "id": 16738203569,
                "nome": "TERMORESISTENCIA THCOM TR300 PT100 B, SIMPLES 3 FI",
                "sku": "400002",
            },
            "categoria": {
                "id": 14852987,
                "nome": "tomadas",
            },
            "payload": {"categorias": [{"id": 14852987}], "situacao": "A"},
            "verificacao_get": "GET /produtos/16738203569",
            "esperado": "Produto deve ter categoria 14852987 após execucao",
        },
        {
            "seq": 2,
            "acao": "arquivar_produto",
            "status": "pendente",
            "tipo": "arquivar",
            "produto": {
                "id": 16735919465,
                "nome": "Conector femea de 1 condutor 231-310_037-000 Wago",
            },
            "payload": {"publicado": False, "situacao": "A"},
            "verificacao_get": "GET /produtos/16735919465",
            "esperado": "Produto deve ter publicado=null/False, situacao=I após execucao",
        },
        {
            "seq": 3,
            "acao": "rollback_arquivamento",
            "status": "pendente",
            "tipo": "rollback",
            "descricao": "Reverte o arquivamento (nao remove categoria adicionada no passo 1)",
            "passos": [
                {"acao": "arquivar_produto", "produto": 16735919465, "publicado": True, "situacao": "A"},
            ],
            "verificacao_get": "GET /produtos/16735919465",
            "esperado": "Produto deve ter publicado=None, situacao=A após rollback",
            "nota": "O produto da acao 1 nao sera revertido automaticamente - limite da API Bling"
        },
    ],
    "resumo": {
        "total_acoes": 3,
        "produtos_envolvidos": 2,
        "produto_atribuir_id": 16738203569,
        "produto_arquivar_id": 16735919465,
        "categoria_atribuir_id": 14852987,
    },
    "historico": []
}

with open(canary_path, 'w', encoding='utf-8') as f:
    json.dump(canary, f, ensure_ascii=False, indent=1)

print(f'Canary salvo em {canary_path}')
print('\nResumo do canary:')
print(f'  Acao 1: Atribuir categoria 14852987 a produto 16738203569')
print(f'  Acao 2: Arquivar produto 16735919465')
print(f'  Acao 3: Rollback do arquivamento (nao remove categoria - limite da API)')