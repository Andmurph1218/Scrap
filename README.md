# Wago Catalog Processor

Pipeline: scrape Wago autenticado → planilha Nuvemshop (template oficial) → pastas de imagens → Anunciar-ML.xlsx preenchido.

Templates oficiais em `templates/`:

- `produtos-Nuvemshop.csv`
- `Anunciar-ML.xlsx`

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# WAGO_USER / WAGO_PASS
```

## Pipeline

```bash
# 1) Links
PYTHONPATH=src python -m wago discover

# 2) Scrape (preço + IPI + specs + URLs de foto)
PYTHONPATH=src python -m wago scrape --limit 3

# 3) (Opcional) reexport auxiliares a partir do master
PYTHONPATH=src python -m wago export

# 4a) Preenche CSV oficial Nuvemshop
PYTHONPATH=src python -m wago fill-nuvemshop

# 4b) Baixa imagens em data/out/imagens/<SKU>__<nome>/
PYTHONPATH=src python -m wago images

# 5) Preenche Anunciar-ML.xlsx (aba default: Outros)
PYTHONPATH=src python -m wago fill-ml --sheet Outros
# PYTHONPATH=src python -m wago fill-ml --sheet Conectores
```

Saídas principais em `data/out/`:

| Arquivo | Etapa |
|---|---|
| `* master.csv` | 2/3 |
| `produtos-Nuvemshop-preenchido.csv` | 4a |
| `imagens/<SKU>__<slug>/` | 4b |
| `Anunciar-ML-preenchido.xlsx` | 5 |

## Testes

```bash
PYTHONPATH=src pytest -q
```
