#!/usr/bin/env python3
"""List all errors from batch lotes."""
import json
import os

dir = 'data/taxonomia/lotes'
all_errors = []
for f in sorted(os.listdir(dir), key=lambda x: int(x.replace('lote-','').replace('.json','')) if x.replace('lote-','').replace('.json','').isdigit() else 0):
    if not f.startswith('lote-') or not f.endswith('.json'):
        continue
    num = f.replace('lote-','').replace('.json','')
    if not num.isdigit():
        continue
    d = json.load(open(os.path.join(dir, f), encoding='utf-8'))
    produtos = d.get('produtos', {})
    if isinstance(produtos, dict):
        erros = produtos.get('erro', [])
    else:
        erros = [p for p in produtos if p.get('status') == 'erro']
    for pe in erros:
        all_errors.append(pe)

http400 = [e for e in all_errors if '400' in e.get('erro','')]
http429 = [e for e in all_errors if '429' in e.get('erro','')]
print(f'Total errors: {len(all_errors)}')
print(f'HTTP 400: {len(http400)}')
print(f'HTTP 429: {len(http429)}')
print()
print('HTTP 400 unique products:')
for e in http400:
    nome = e.get('nome', '')[:60]
    print(f'  {e["produto"]} - {nome}')
print()
print('HTTP 429 unique products:')
for e in http429:
    nome = e.get('nome', '')[:60]
    print(f'  {e["produto"]} - {nome}')