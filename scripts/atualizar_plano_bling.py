#!/usr/bin/env python3
"""Atualizar plano-bling-real.json para usar payload Bling correto.

O plano original usa Nuvemshop-style (`categories` array de IDs).
Precisamos converter para Bling v3 (`categorias` array de objetos {id}).
"""

import json
from pathlib import Path
from datetime import datetime


def main():
    plano_path = Path('data/taxonomia/planos/plano-bling-real.json')
    
    with open(plano_path, 'r', encoding='utf-8') as f:
        plano = json.load(f)
    
    print(f'Plano original: {plano.get("resumo", {}).get("total_acoes", 0)} acoes')
    
    # Atualizar endpoint e payload para Bling
    for acao in plano.get('fases', {}).get('mover', []):
        if 'payload' in acao:
            ids = acao['payload'].get('categories', [])
            # Converter para formato Bling: categorias como array de {id}
            acao['payload'] = {
                'categorias': [{'id': c} for c in ids],
                'situacao': 'A'  # Ativo por padrao
            }
            # Atualizar endpoint para Bling
            acao['endpoint'] = f"PUT /produtos/{acao['produto']}"
        acao['acao'] = 'atribuir_categoria'  # Renomear para clareza
    
    # Atualizar resumo
    plano['resumo']['total_acoes'] = len(plano['fases']['mover'])
    plano['resumo']['mover'] = len(plano['fases']['mover'])
    plano['gerado_em'] = datetime.now().isoformat(timespec='seconds')
    
    # Salvar
    with open(plano_path, 'w', encoding='utf-8') as f:
        json.dump(plano, f, ensure_ascii=False, indent=1)
    
    print(f'Plano atualizado: {plano.get("resumo", {}).get("total_acoes", 0)} acoes')
    print(f'Payload padronizado para Bling v3')


if __name__ == '__main__':
    main()