# Wago Catalog Processor

Pipeline unificado para coletar produtos Wago (preço autenticado + IPI), exportar planilhas (Nuvemshop / Bling / Mercado Livre), vincular fotos na Nuvemshop e gerar templates de categoria do ML.

> Os PDFs em [wago.com/br/catalogos](https://www.wago.com/br/catalogos) são material de especificação. A fonte de SKUs/preços deste projeto é o **sitemap de produtos** + páginas `/p/` com login.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows WSL: igual
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# edite .env com WAGO_USER / WAGO_PASS (e demais tokens conforme a etapa)
```

## Comandos

```bash
# 1) Coleta links do sitemap
PYTHONPATH=src python -m wago discover

# 2) Scrape autenticado (P0: preço estável + limpa carrinho após IPI)
PYTHONPATH=src python -m wago scrape --limit 3

# 3) Reexporta CSVs a partir de um master
PYTHONPATH=src python -m wago export "data/out/wago lote 1 DD-MM master.csv"

# 4) Após importar o CSV na Nuvemshop, vincula fotos (fail-fast de credenciais)
PYTHONPATH=src python -m wago photos

# 5) Mercado Livre — Etapa 1 (mapa geral) + templates
PYTHONPATH=src python -m wago ml-map --max-nodes 20
PYTHONPATH=src python -m wago ml-templates --limit 5
```

Saídas em `data/out/`. Estado (SKUs já processados, blacklist sem preço, auth Playwright) em `data/state/`.

## P0 corrigidos neste pacote

1. **Preço** — prioridade JSON-LD → `data-price` → JS BRL → seletor `.price`. Não usa mais `max()` de todos os `R$` da página.
2. **IPI** — após ler o carrinho, chama `limpar_carrinho()` para não acumular itens.
3. **ML Etapa 1** — `ml-map` gera `data/maps/mapa_geral_categorias_ml.json` (faltava nos scripts originais).
4. **Fotos** — `require_env` falha imediatamente se `NUVEMSHOP_*` estiver ausente; pula produtos que já têm imagens.

## Variáveis de ambiente

| Variável | Etapa |
|---|---|
| `WAGO_USER` / `WAGO_PASS` | `scrape` |
| `MARGIN_DIVISOR` (default `0.6`) | `scrape` |
| `NUVEMSHOP_STORE_ID` / `NUVEMSHOP_ACCESS_TOKEN` | `photos` |
| `ML_CLIENT_ID` / `ML_CLIENT_SECRET` / `ML_REFRESH_TOKEN` | `ml-map`, `ml-templates` |

## Testes

```bash
pip install pytest
PYTHONPATH=src pytest -q
```
