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
# 1) Links (reusa data/raw/links_wago_coletados.json; --refresh baixa de novo)
PYTHONPATH=src python -m wago discover
PYTHONPATH=src python -m wago discover --refresh

# 2) Scrape (preço + IPI + specs + URLs de foto)
PYTHONPATH=src python -m wago scrape --limit 3

# 3) (Opcional) reexport auxiliares a partir do master
PYTHONPATH=src python -m wago export

# 4a) Preenche CSV oficial Nuvemshop
PYTHONPATH=src python -m wago fill-nuvemshop
PYTHONPATH=src python -m wago fill-nuvemshop --dry-run
PYTHONPATH=src python -m wago fill-nuvemshop --force

# 4b) Baixa imagens em data/out/imagens/<SKU>__<slug>/
PYTHONPATH=src python -m wago images
PYTHONPATH=src python -m wago images --dry-run

# 5) Preenche Anunciar-ML.xlsx (aba default: Outros)
PYTHONPATH=src python -m wago fill-ml --sheet Outros
PYTHONPATH=src python -m wago fill-ml --sheet Conectores
PYTHONPATH=src python -m wago fill-ml --dry-run
```

## IPI e preço

Venda = `(custo + IPI) / MARGIN_DIVISOR` (`0.6` por padrão).

IPI `0.00` medido no carrinho é valor real e entra no preço. Falha de consulta (Bearer ausente, timeout, HTTP inválido ou SKU fora do carrinho) não calcula preço, não grava o produto e não marca o SKU como processado: ele continua pendente. Entrada encontrada no carrinho sem tributo de IPI é tratada como zero e o log deixa isso explícito.

## Formatos de saída

CSV com separador `;` e encoding `utf-8-sig`.

| Arquivo | Conteúdo |
|---|---|
| `data/raw/links_wago_coletados.json` | Lista de URLs do sitemap |
| `data/state/skus_processados.txt` | SKU confirmado, uma linha, só depois do checkpoint |
| `data/state/skus_sem_preco.txt` | SKU sem custo publicado; não entra de novo no scrape |
| `data/state/checkpoint_master_lote_<n>.csv` | Master recuperável do lote. Substituição só depois da gravação temporária |
| `data/out/wago lote <n> <dd-mm> master.csv` | Master do lote: uma linha por SKU, com `SKU`, `Nome`, `Custo`, `Preço`, `IPI`, `Estoque`, `Fotos`, medidas, descrição e colunas da Nuvemshop |
| `data/out/wago lote <n> <dd-mm> nuvemshop.csv` | Recorte auxiliar das colunas Nuvemshop presentes no master |
| `data/out/wago lote <n> <dd-mm> bling.csv` | Export auxiliar Bling |
| `data/out/wago lote <n> <dd-mm> mercadolivre.csv` | Export auxiliar; o arquivo oficial da etapa 5 é o XLSX |
| `data/out/produtos-Nuvemshop-preenchido.csv` | Import oficial, 31 colunas do template |
| `data/out/imagens/<SKU>__<slug>/` | Fotos locais |
| `data/out/Anunciar-ML-preenchido.xlsx` | Planilha oficial preenchida |

O master só aceita linha com SKU, nome, custo e preço maiores que zero, e IPI numérico maior ou igual a zero. Se o processo cair depois do checkpoint, o próximo `scrape` do mesmo `--lote` recarrega essas linhas, evita outro scrape do SKU e reexporta o master.

## Exportação

`fill-nuvemshop` lê o cabeçalho de `templates/produtos-Nuvemshop.csv` (31 colunas, na ordem do arquivo) e descarta as linhas de exemplo. Preço divergente de `(custo + IPI) / MARGIN_DIVISOR`, IPI ausente, medida inválida e SKU repetido ficam de fora. O resumo sai no terminal e em `*.resumo.json`. `--dry-run` não grava. Um arquivo de saída já existente só é trocado com `--force`.

`images` cria `data/out/imagens/<SKU>__<slug>/01.jpg`, `02.png`, etc. A extensão segue o conteúdo do arquivo, não o sufixo da URL. Download válido não é repetido nem sobrescrito. Timeout, HTTP 429/5xx e falha de rede tentam de novo até `--tentativas` (padrão 3). O relatório é `resumo-imagens.json`.

`fill-ml` altera só as células da aba pedida dentro de uma cópia do `templates/Anunciar-ML.xlsx`. Fórmulas (contagem de caracteres, tarifa, BUYBOX), validações, imagens da aba Ajuda e as outras abas permanecem. Dimensões vão em centímetros inteiros. O peso fica em quilogramas, com até 3 decimais, como a planilha pede. Colunas exclusivas de abas como Conectores não são inventadas. `--force`, quando o arquivo de saída já existe, acrescenta SKUs novos nessa cópia em vez de apagar os que já estão na aba.

## Dashboard local

```bash
PYTHONPATH=src python -m wago dashboard
PYTHONPATH=src python -m wago dashboard --port 8765 --open
```

O painel abre em `http://127.0.0.1:8765/` e escuta só nessa máquina. Não use `0.0.0.0`: quem estiver na rede local poderia disparar o scrape e gravar arquivos. Quem usa o mesmo computador também pode, porque não há login. Se a porta já estiver em uso, o processo para com uma mensagem em vez de dividir a porta com outra instância.

O histórico fica em `data/state/dashboard.sqlite` e continua depois de reiniciar. Há uma execução por vez. Cancelar pede parada entre produtos ou imagens; o arquivo que já está sendo gravado termina. Credenciais não aparecem na tela nem nos eventos. Arquivos baixados pela interface precisam estar dentro de `data/out`.

## Homologação

O primeiro lote deve ser pequeno. Nada disso publica anúncio nem envia foto para a Nuvemshop.

```bash
PYTHONPATH=src python -m wago discover
PYTHONPATH=src python -m wago scrape --limit 3 --lote 5
PYTHONPATH=src python -m wago fill-nuvemshop "data/out/wago lote 5 <data> master.csv" -o data/out/homologacao-nuvemshop.csv
PYTHONPATH=src python -m wago images "data/out/wago lote 5 <data> master.csv" --limit 3
PYTHONPATH=src python -m wago fill-ml "data/out/wago lote 5 <data> master.csv" --sheet Outros -o data/out/homologacao-ml.xlsx
```

`photos`, `ml-map` e `ml-templates` ficam de fora: usam API externa e alterariam conta ou catálogo.

Na conferência de 23/10/2026 o sitemap local tinha 19.804 links. O lote 5, com limite 3, autenticou na Wago e gravou os SKUs 890-606, 2106-1204 e 248-504 em `data/out/wago lote 5 23-10 master.csv`. Vários links foram pulados por não terem preço de custo publicado; três ficaram pendentes porque o IPI não voltou do carrinho e não entraram no master. O CSV `data/out/homologacao-nuvemshop.csv` saiu com 31 colunas e os três preços da fórmula. As imagens ficaram em `data/out/imagens/`, 15 arquivos em 3 pastas. `data/out/homologacao-ml.xlsx` recebeu os três SKUs na aba Outros, com fórmulas, validações e as 9 mídias do template. O arquivo oficial `templates/Anunciar-ML.xlsx` não foi alterado. Nada foi publicado na Nuvemshop nem no Mercado Livre.

## Testes

```bash
PYTHONPATH=src pytest -q
```

A suíte usa fixtures sintéticas. Não faz login na Wago, não baixa o sitemap e não publica em Nuvemshop nem Mercado Livre.

`tests/test_fase4_integrado.py` percorre o miolo do pipeline num só cenário: grava um checkpoint, recupera o lote, gera o CSV de 31 colunas, baixa imagens com HTTP simulado e preenche duas abas do XLSX. Cada arquivo é fechado e aberto de novo antes da conferência. O mesmo módulo sobe o painel em `127.0.0.1`, recusa porta inválida, porta ocupada, parâmetro sem confirmação, aba inexistente e caminho fora de `data/out`, impede a segunda execução e, depois do `shutdown`, confirma que a porta não responde e que o histórico continua no SQLite.
