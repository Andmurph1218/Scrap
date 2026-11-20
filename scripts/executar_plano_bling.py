#!/usr/bin/env python3
"""Executar plano-bling-real.json - classificacao de produtos.

Este script executa as acoes de atribuir_categoria do plano no Bling v3.

IMPORTANTE: O PUT do Bling v3:
- Permite ATtribuir categoria (via `categorias` array)
- NÃO permite REMOVER categoria (categorias=[] é ignorado)
- O produto mantem a categoria mesmo se não enviada no PUT

Por isso, este script gera logs detalhados de cada acao.
"""

import json
from datetime import datetime
from pathlib import Path

from wago.marketplaces import criar_provider
from wago.marketplaces.bling_provider import _dicionario, _ids_produto


def main():
    plano_path = Path('data/taxonomia/planos/plano-bling-real.json')
    
    with open(plano_path, 'r', encoding='utf-8') as f:
        plano = json.load(f)
    
    print('=' * 60)
    print('EXECUTANDO PLANO-BLING-REAL.JSON')
    print('=' * 60)
    print(f'Total acoes: {plano["resumo"]["total_acoes"]}')
    print(f'Acoes de atribuir: {plano["resumo"]["mover"]}')
    
    provider = criar_provider('bling')
    print('\nProvider Bling conectado\n')
    
    # Executar apenas algumas acoes para teste (ou todas)
    acoes = plano['fases']['mover']
    max_acoes = 10  # Processar apenas primeiras 10 para teste
    
    resultados = {
        'sucesso': [],
        'falha': [],
        'parcial': [],
    }
    
    for i, acao in enumerate(acoes[:max_acoes]):
        pid = acao['produto']
        cat_ids = [c['id'] for c in acao['payload']['categorias']]
        
        print(f'[{i+1}/{max_acoes}] Produto {pid}: categoria {cat_ids}')
        
        try:
            # GET produto
            resp = provider.client.requisitar('GET', f'/produtos/{pid}')
            prod = _dicionario(resp)
            ids_atual = _ids_produto(prod)
            
            # Verificar se precisa mudar
            if set(ids_atual) == set(cat_ids):
                print(f'  [OK] Categoria ja esta correta: {ids_atual}')
                resultados['sucesso'].append({'produto': pid, 'motivo': 'ja_atribuido'})
                continue
            
            # PUT atribuir categoria
            provider.atualizar_produto(pid, {'categorias': [{'id': c} for c in cat_ids]})
            
            # GET verificacao
            resp = provider.client.requisitar('GET', f'/produtos/{pid}')
            prod_verif = _dicionario(resp)
            ids_verif = _ids_produto(prod_verif)
            
            if set(ids_verif) == set(cat_ids):
                print(f'  [OK] Atribuido: {ids_verif}')
                resultados['sucesso'].append({'produto': pid, 'categoria': cat_ids})
            else:
                print(f'  [AVISO] Nao converteu: esperado {cat_ids}, obteve {ids_verif}')
                resultados['parcial'].append({'produto': pid, 'esperado': cat_ids, 'obteve': ids_verif})
                
        except Exception as e:
            print(f'  [ERRO] {e}')
            resultados['falha'].append({'produto': pid, 'erro': str(e)})
    
    print('\n' + '=' * 60)
    print('RESUMO')
    print('=' * 60)
    print(f'Sucesso: {len(resultados["sucesso"])}')
    print(f'Parcial: {len(resultados["parcial"])}')
    print(f'Falha: {len(resultados["falha"])}')
    
    # Salvar resultados parciais
    output = {
        'executado_em': datetime.now().isoformat(timespec='seconds'),
        'total_processado': min(len(acoes), max_acoes),
        'total_plano': len(acoes),
        'resultados': resultados,
    }
    
    output_path = Path('data/taxonomia/planos/execucao-bling-parcial.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=1)
    
    print(f'\nResultados salvos em: {output_path}')


if __name__ == '__main__':
    main()