#!/usr/bin/env python3
"""Reprocessar os 230 erros dos lotes Bling.

Opção A: Reprocessamento único - 429 devem passar, 400 vão para exceções como falha_permanente.
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Carregar .env antes de importar de wago
from dotenv import load_dotenv
load_dotenv('.env', override=True)

from wago.marketplaces import criar_provider
from wago.marketplaces.base import ErroMarketplace, ErroRateLimit


def carregar_erros():
    """Carrega todos os erros dos arquivos de lote."""
    dir_lotes = Path('data/taxonomia/lotes')
    all_errors = []
    
    for f in sorted(os.listdir(dir_lotes), key=lambda x: int(x.replace('lote-','').replace('.json','')) if x.replace('lote-','').replace('.json','').isdigit() else 0):
        if not f.startswith('lote-') or not f.endswith('.json'):
            continue
        num = f.replace('lote-','').replace('.json','')
        if not num.isdigit():
            continue
        d = json.load(open(os.path.join(dir_lotes, f), encoding='utf-8'))
        produtos = d.get('produtos', {})
        if isinstance(produtos, dict):
            erros = produtos.get('erro', [])
        else:
            erros = [p for p in produtos if p.get('status') == 'erro']
        for pe in erros:
            all_errors.append(pe)
    
    return all_errors


def buscar_categoria_no_plano(produto_id):
    """Busca a categoria alvo para um produto no plano de migração."""
    plano_path = Path('data/taxonomia/planos/plano-bling-real.json')
    with open(plano_path, 'r', encoding='utf-8') as f:
        plano = json.load(f)
    
    for a in plano['fases']['mover']:
        if a['produto'] == produto_id:
            return a['para'][0] if isinstance(a['para'], list) else a['para']
    return None


def adicionar_excecao(lista_excecoes, produto_id, motivo):
    """Adiciona um produto à lista de exceções."""
    excecao = {
        'id': produto_id,
        'categoria_correta': None,
        'motivo': motivo,
        'data_correcao': datetime.now().date().isoformat()
    }
    lista_excecoes.append(excecao)


def main():
    print('=' * 60)
    print('REPROCESSAMENTO DE ERROS - BLING TAXONOMIA')
    print('=' * 60)
    
    erros = carregar_erros()
    print(f'Total de erros encontrados: {len(erros)}')
    
    http400 = [e for e in erros if '400' in e.get('erro','')]
    http429 = [e for e in erros if '429' in e.get('erro','')]
    outros = [e for e in erros if '400' not in e.get('erro','') and '429' not in e.get('erro','')]
    
    print(f'HTTP 400: {len(http400)}')
    print(f'HTTP 429: {len(http429)}')
    print(f'Outros: {len(outros)}')
    print()
    
    if not erros:
        print('Nenhum erro para reprocessar.')
        return
    
    # Conectar ao provider
    print('Conectando ao Bling...')
    provider = criar_provider('bling')
    print('Provider Bling conectado\n')
    
    # Preparar lista de exceções
    excecoes_path = Path('data/taxonomia/excecoes/lista_excecoes.json')
    excecoes_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Carregar exceções existentes se houver
    if excecoes_path.exists():
        with open(excecoes_path, 'r', encoding='utf-8') as f:
            dados_excecoes = json.load(f)
        lista_excecoes = dados_excecoes.get('produtos_excecao', [])
    else:
        lista_excecoes = []
    
    # Conjunto de IDs já em exceções para não duplicar
    ids_excecoes_existentes = {e['id'] for e in lista_excecoes}
    
    resultados = {
        'reprocessados_com_sucesso': [],
        'falha_permanente': [],
        'ja_em_excecoes': [],
    }
    
    print(f'Iniciando reprocessamento de {len(erros)} produtos...')
    print()
    
    for i, erro in enumerate(erros, 1):
        produto_id = erro['produto']
        nome = erro.get('nome', '')
        erro_msg = erro.get('erro', '')
        
        print(f'[{i}/{len(erros)}] Produto {produto_id} - {nome[:50]}...')
        
        # Verificar se já está em exceções
        if produto_id in ids_excecoes_existentes:
            print(f'  -> Já em exceções, pulando')
            resultados['ja_em_excecoes'].append(produto_id)
            continue
        
        # Buscar categoria alvo no plano
        categoria_id = buscar_categoria_no_plano(produto_id)
        
        if not categoria_id:
            print(f'  -> Ação não encontrada no plano, marcando como falha_permanente')
            adicionar_excecao(lista_excecoes, produto_id, 'Ação não encontrada no plano de migração')
            resultados['falha_permanente'].append(produto_id)
            continue
        
        # Tentar atualizar
        print(f'  -> Tentando atribuir categoria {categoria_id}...')
        try:
            provider.atualizar_produto(produto_id, {'categorias': [{'id': categoria_id}]})
            print(f'  -> SUCESSO!')
            resultados['reprocessados_com_sucesso'].append(produto_id)
            
        except ErroRateLimit as e:
            # Rate limit - aguardar e tentar novamente uma vez
            print(f'  -> Rate limit (429): {e}')
            print(f'  -> Aguardando 5 segundos e tentando novamente...')
            time.sleep(5)
            try:
                provider.atualizar_produto(produto_id, {'categorias': [{'id': categoria_id}]})
                print(f'  -> SUCESSO na retentativa!')
                resultados['reprocessados_com_sucesso'].append(produto_id)
            except Exception as e2:
                print(f'  -> FALHA PERMANENTE após retentativa: {e2}')
                adicionar_excecao(lista_excecoes, produto_id, 'Rate limit persistiu após retentativa')
                resultados['falha_permanente'].append(produto_id)
                
        except ErroMarketplace as e:
            # Erro 400 ou outro erro do marketplace
            if e.status == 400:
                print(f'  -> HTTP 400 (falha permanente): {e.mensagem}')
                adicionar_excecao(lista_excecoes, produto_id, 'Erro de validação do Bling (ex: Variedura sem variações)')
                resultados['falha_permanente'].append(produto_id)
            else:
                print(f'  -> Erro Marketplace ({e.status}): {e.mensagem}')
                adicionar_excecao(lista_excecoes, produto_id, f'Erro Marketplace HTTP {e.status}')
                resultados['falha_permanente'].append(produto_id)
                
        except Exception as e:
            print(f'  -> Erro inesperado: {e}')
            adicionar_excecao(lista_excecoes, produto_id, 'Erro inesperado durante reprocessamento')
            resultados['falha_permanente'].append(produto_id)
        
        # Pequena pausa para não sobrecarregar a API
        time.sleep(0.2)
    
    # Salvar lista de exceções atualizada
    dados_saida = {
        'versao': '1.0',
        'gerado_em': datetime.now().isoformat(),
        'produtos_excecao': lista_excecoes,
        'total_excecoes': len(lista_excecoes)
    }
    with open(excecoes_path, 'w', encoding='utf-8') as f:
        json.dump(dados_saida, f, ensure_ascii=False, indent=2)
    
    print()
    print('=' * 60)
    print('RESULTADO DO REPROCESSAMENTO')
    print('=' * 60)
    print(f'Reprocessados com sucesso: {len(resultados["reprocessados_com_sucesso"])}')
    print(f'Falha permanente (adicionados a exceções): {len(resultados["falha_permanente"])}')
    print(f'Já existentes em exceções: {len(resultados["ja_em_excecoes"])}')
    print(f'Total de exceções no arquivo: {len(lista_excecoes)}')
    print(f'Arquivo salvo em: {excecoes_path}')
    
    # Verificar recuperação dos 429
    recuperados_429 = [p for p in resultados['reprocessados_com_sucesso'] if any(e['produto'] == p and '429' in e.get('erro','') for e in erros)]
    print(f'\nHTTP 429 recuperados: {len(recuperados_429)}/{len(http429)}')


if __name__ == '__main__':
    main()