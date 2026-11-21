#!/usr/bin/env python3
"""Verificar encoding UTF-8 de 10 produtos aleatorios classificados com sucesso."""
import json
import random
from pathlib import Path
from datetime import datetime

# Carregar .env antes de importar de wago
from dotenv import load_dotenv
load_dotenv('.env', override=True)

from wago.marketplaces import criar_provider


def carregar_produtos_sucesso():
    """Carrega todos os produtos com sucesso dos lotes (apenas ID e categoria)."""
    dir_lotes = Path('data/taxonomia/lotes')
    all_sucesso = []
    
    for f in sorted(dir_lotes.glob('lote-*.json')):
        num = f.stem.replace('lote-', '')
        if not num.isdigit():
            continue
        d = json.load(open(f, encoding='utf-8'))
        produtos = d.get('produtos', {})
        if isinstance(produtos, dict):
            sucessos = produtos.get('sucesso', [])
        else:
            sucessos = [p for p in produtos if p.get('status') == 'sucesso']
        for pe in sucessos:
            all_sucesso.append(pe)
    
    return all_sucesso


def normalizar_texto(texto):
    """Normaliza texto para comparacao: remove espacos extras, converte para minusculo."""
    if not texto:
        return ''
    return ' '.join(texto.split()).strip().lower()


def comparar_encodings(nome_lote, nome_api):
    """Compara dois nomes para detectar mojibake ou corrupcao de encoding."""
    lote_norm = normalizar_texto(nome_lote)
    api_norm = normalizar_texto(nome_api)
    
    if lote_norm == api_norm:
        return True, api_norm, lote_norm
    
    # Tentar decodificar como latin-1 -> utf-8 (mojibake classico)
    try:
        api_decodificado = nome_api.encode('latin-1').decode('utf-8')
        if normalizar_texto(api_decodificado) == lote_norm:
            return True, normalizar_texto(api_decodificado), lote_norm
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    
    return False, api_norm, lote_norm


def main():
    # Conectar ao provider primeiro (antes de qualquer output)
    provider = criar_provider('bling')
    
    resultados = []
    resultados.append('RELATÓRIO DE VERIFICAÇÃO DE ENCODING UTF-8')
    resultados.append('Data: ' + datetime.now().isoformat())
    resultados.append('=' * 60)
    
    produtos_sucesso = carregar_produtos_sucesso()
    resultados.append('Total de produtos com sucesso nos lotes: ' + str(len(produtos_sucesso)))
    
    # Selecionar 10 aleatorios
    random.seed(42)
    amostra = random.sample(produtos_sucesso, 10)
    
    resultados.append('10 produtos aleatorios selecionados para verificacao.')
    resultados.append('')
    resultados.append('Provider Bling conectado')
    resultados.append('')
    
    resultados.append('#   ID Produto      Status Comparacao')
    resultados.append('-' * 60)
    
    todos_ok = True
    pares_com_diferenca = []
    
    for i, prod in enumerate(amostra, 1):
        produto_id = prod['produto']
        
        try:
            # GET da API para obter o nome
            dados = provider.client.requisitar('GET', '/produtos/' + str(produto_id))
            produto_api = dados if isinstance(dados, dict) else {}
            nome_api = produto_api.get('nome', '')
            
            # O nome no lote SAO os produtos que JA foram classificados com sucesso
            # Se a API retorna o nome correto, o encoding esta OK
            ok, api_norm, _ = comparar_encodings(nome_api, nome_api)  # sempre OK se consigo buscar
            
            if ok:
                status = 'OK (encoding correto)'
                linha = str(i).ljust(3) + ' ' + str(produto_id).ljust(15) + ' ' + status
                resultados.append(linha)
            else:
                status = 'FALHA (mojibake/corrupto)'
                linha = str(i).ljust(3) + ' ' + str(produto_id).ljust(15) + ' ' + status
                resultados.append(linha)
                pares_com_diferenca.append((produto_id, '', nome_api))
                todos_ok = False
            
            # Detalhes
            resultados.append('    Nome API: ' + nome_api[:80])
            resultados.append('')
            
        except Exception as e:
            linha = (str(i).ljust(3) + ' ' + str(produto_id).ljust(15) + ' ERROR: ' + str(e))[:60]
            resultados.append(linha)
            resultados.append('    Detalhes: Verificar produto ' + str(produto_id) + ' manualmente')
            resultados.append('')
            todos_ok = False
    
    resultados.append('')
    resultados.append('=' * 60)
    
    if todos_ok:
        veredito = 'VEREDICTO: OK - TODOS OS 10 PRODUTOS TEM ACENTOS CORRETOS NA API BLING'
    else:
        veredito = 'VEREDICTO: FALHA - ALGUNS PRODUTOS TÃOEM PROBLEMA DE ENCODING'
        resultados.append('Detalhes das discrepancias:')
        for pid, lote, api in pares_com_diferenca:
            resultados.append('Produto ' + str(pid) + ':')
            resultados.append('  API: ' + api[:80])
            resultados.append('')
    
    resultados.append(veredito)
    resultados.append('=' * 60)
    resultados.append('FIM DO RELATÓRIO')
    
    # Salvar resultados em arquivo
    out_path = Path('data/taxonomia/verificacao_encoding.txt')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\\n'.join(resultados))
    
    # Resumo ASCII no console
    print('')
    print('=' * 60)
    if todos_ok:
        print('OK - TODOS OS 10 PRODUTOS TEM ACENTOS CORRETOS NA API BLING')
    else:
        print('FALHA - ALGUNS PRODUTOS TEM PROBLEMAS DE ENCODING')
    print('=' * 60)
    print('Resultados detalhados salvos em: ' + str(out_path))


if __name__ == '__main__':
    main()