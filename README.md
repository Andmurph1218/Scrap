# Wago Catalog Processor

Pipeline local: login Wago → discover de links → scrape autenticado (preço + IPI + specs + fotos) → master CSV → planilha oficial Nuvemshop → download de imagens → Anunciar-ML.xlsx → vínculo de fotos na Nuvemshop via API.

Tudo roda pela CLI ou pelo dashboard local (`http://127.0.0.1:8765/`), com progresso resumível por checkpoints. Nada é publicado automaticamente em plataforma externa.

Templates oficiais em `templates/`:

- `produtos-Nuvemshop.csv`
- `Anunciar-ML.xlsx`

## Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/pip install -e .
.venv/Scripts/playwright install chromium
cp .env.example .env
```

`.env` (fica fora do Git):

| Variável | Uso |
|---|---|
| `WAGO_USER` / `WAGO_PASS` | Login no webshop Wago |
| `MARGIN_DIVISOR` | Divisor da margem (padrão `0.6`) |
| `NUVEMSHOP_STORE_ID` | ID da loja (ex.: `6667412`) |
| `NUVEMSHOP_ACCESS_TOKEN` | Token do app sob medida Nuvemshop |
| `NUVEMSHOP_USER_AGENT` | Ex.: `JHSHOPPING (contato@jhshopping.com.br)` |
| `NUVEMSHOP_CLIENT_ID` / `NUVEMSHOP_CLIENT_SECRET` | Opcional: fluxo OAuth automático (`nuvemshop-auth`) |

## Execução rápida

```bash
# Dashboard (recomendado)
.venv/Scripts/python.exe -m wago dashboard --port 8765 --open

# ou duplo clique em WagoPipeline.exe (launcher Windows na raiz)
```

O `WagoPipeline.exe` é um inicializador nativo: usa o `.venv` e o código de `src/wago` direto, então atualizações no código Python valem automaticamente, sem recompilar. Se o painel já estiver no ar, ele apenas abre o navegador.

## Pipeline (CLI)

```bash
# 1) Links (reusa data/raw/links_wago_coletados.json; --refresh baixa de novo)
python -m wago discover
python -m wago discover --refresh

# 2) Scrape autenticado (preço via API Wago + IPI do carrinho + specs + URLs de foto)
python -m wago scrape --limit 3

# 3) Reexporta auxiliares a partir do master
python -m wago export

# 4a) CSV oficial Nuvemshop (31 colunas do template)
python -m wago fill-nuvemshop [--dry-run] [--force]

# 4b) Imagens em data/out/imagens/<SKU>__<slug>/
python -m wago images [--dry-run] [--limit N]

# 5) Anunciar-ML.xlsx (aba default: Outros; aceita produto sem SKU)
python -m wago fill-ml --sheet Outros [--dry-run] [--force]

# 6) Vincula fotos na Nuvemshop via API (Base64, alta resolução, sem duplicatas)
python -m wago photos [--dry-run] [--force]

# Autorização OAuth Nuvemshop (grava token no .env da raiz)
python -m wago nuvemshop-auth
```

## Estado por SKU (catálogo)

Cada SKU tem um status em `data/state/catalogo.sqlite`, que é a fonte da verdade do progresso:

```
DESCOBERTO -> AGUARDANDO_SCRAPE -> SCRAPE_OK -> AGUARDANDO_IMAGEM -> EXPORTADO
                               \-> FILTRADO_SEM_CUSTO (terminal)
```

- `discover` insere SKUs novos como `DESCOBERTO`; o `scrape` enfileira (`AGUARDANDO_SCRAPE`) e consome a fila numa ordem embaralhada **uma vez** e persistida. Se o processo cair no item 200, a próxima execução começa do 201.
- Falha no scrape (IPI, timeout, checkpoint) mantém o SKU na fila, incrementa `tentativas`, grava `ultimo_erro` e o manda para o fim da fila.
- `SCRAPE_OK` → `AGUARDANDO_IMAGEM` → `EXPORTADO` é derivado das saídas em disco (CSV Nuvemshop e pastas de imagem) depois de `fill-nuvemshop` e `images`.
- Transições fora do grafo são ignoradas: repetir uma etapa não regride nem duplica SKU.
- `skus_processados.txt` e `skus_sem_preco.txt` continuam sendo gravados como espelho legível e são importados na primeira abertura do catálogo.

`python -m wago status` mostra a contagem por status.

## Dashboard local

Escuta só em `127.0.0.1` (nunca `0.0.0.0`). Histórico e fila em `data/state/dashboard.sqlite`. Uma execução por vez; cancelar pede parada entre itens.

O botão **Executar** só enfileira. Quem roda é o worker (`python -m wago worker`), um processo separado que o painel sobe sozinho quando não há worker vivo e que se encerra após 2 min sem trabalho (log em `data/state/worker.log`). Fechar o painel não interrompe a execução. Se o worker morrer no meio (sem batimento por 60 s), a execução vira `interrompida` e basta executar de novo: o catálogo retoma do ponto em que parou.

Etapas no painel:

1. **Discover links** — sitemap Wago
2. **Scrape autenticado** — continua de onde parou (checkpoints)
3. **Export master** — reexporta auxiliares
4. **Preencher Nuvemshop** — CSV oficial
5. **Baixar imagens** — pastas por SKU
6. **Preencher Mercado Livre** — XLSX oficial
7. **Vincular fotos Nuvemshop** — API externa; exige CSV já importado na loja e credenciais no `.env`

O painel mostra cards de status, contadores (processados/exportados, contados por `csv.reader`; "Produtos ignorados (sem custo)" são os SKUs de `skus_sem_preco.txt`, pulados por regra e não por erro), um funil que indica onde os produtos descobertos estão parados (fila do scrape, ignorados, master, CSV Nuvemshop, imagens) e destaca o gargalo, log de eventos com os mais novos no topo e botão **Resetar contadores** que limpa progresso e saídas somente após confirmação.

## Comportamento do scrape

- Preço de custo vem de `POST wagoapi/v2/br-wago/prices` com `commerce-token` autenticado; há fallback de espera no DOM (`wg-price`). Token expirado (401/403) renova o login e repete. Só entra em `skus_sem_preco.txt` quando a API confirma valor `0.0`.
- O carrinho é limpo após a consulta de IPI de cada produto e ao final do lote (`DELETE .../carts/current/removeEntries`).
- Categoria Nuvemshop: apenas a categoria principal em português. Descrição: apenas a descrição curta do item (+ código/brand/garantia). Dados técnicos ficam nas colunas próprias.

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
| `data/state/catalogo.sqlite` | Status de cada SKU (fonte da verdade do progresso) |
| `data/state/dashboard.sqlite` | Histórico e fila de execuções do painel |
| `data/state/worker.log` | Saída do worker em background |
| `data/out/wago lote <n> <dd-mm> master.csv` | Master do lote: uma linha por SKU |
| `data/out/wago lote <n> <dd-mm> nuvemshop.csv` | Recorte auxiliar das colunas Nuvemshop |
| `data/out/wago lote <n> <dd-mm> bling.csv` | Export auxiliar Bling |
| `data/out/wago lote <n> <dd-mm> mercadolivre.csv` | Export auxiliar; o oficial é o XLSX |
| `data/out/produtos-Nuvemshop-preenchido.csv` | Import oficial, 31 colunas do template |
| `data/out/imagens/<SKU>__<slug>/` | Fotos locais |
| `data/out/Anunciar-ML-preenchido.xlsx` | Planilha oficial preenchida |
| `data/out/resumo-fotos-nuvemshop.json` | Relatório do vínculo de fotos |

O master só aceita linha com SKU, nome, custo e preço maiores que zero, e IPI numérico maior ou igual a zero. Se o processo cair depois do checkpoint, o próximo `scrape` do mesmo `--lote` recarrega essas linhas, evita outro scrape do SKU e reexporta o master.

## Exportação

`fill-nuvemshop` lê o cabeçalho de `templates/produtos-Nuvemshop.csv` (31 colunas, na ordem do arquivo) e descarta as linhas de exemplo. Preço divergente de `(custo + IPI) / MARGIN_DIVISOR`, IPI ausente, medida inválida e SKU repetido ficam de fora. O resumo sai no terminal e em `*.resumo.json`. `--dry-run` não grava. Um arquivo de saída já existente só é trocado com `--force`.

`images` cria `data/out/imagens/<SKU>__<slug>/01.jpg`, `02.png`, etc. A extensão segue o conteúdo do arquivo, não o sufixo da URL. Download válido não é repetido nem sobrescrito. Timeout, HTTP 429/5xx e falha de rede tentam de novo até `--tentativas` (padrão 3). O relatório é `resumo-imagens.json`.

`fill-ml` altera só as células da aba pedida dentro de uma cópia do `templates/Anunciar-ML.xlsx`. Fórmulas (contagem de caracteres, tarifa, BUYBOX), validações, imagens da aba Ajuda e as outras abas permanecem. Dimensões vão em centímetros inteiros. O peso fica em quilogramas, com até 3 decimais. Produto sem SKU é exportado normalmente com o campo em branco — SKU não é requisito nem chave de duplicidade. `--force`, quando o arquivo de saída já existe, acrescenta linhas novas em vez de apagar as que já estão na aba.

`photos` varre os produtos da loja paginado (`per_page=200`), mapeia SKU→product_id pelas variantes e envia cada foto como `attachment` Base64 em `POST /v1/{store}/products/{id}/images`. Filtra miniaturas/baixa resolução e remove duplicatas por hash; produtos que já têm imagens são pulados, salvo `--force` (ou quando há lixo antigo, que é limpo antes de subir). `--dry-run` só conta o que seria enviado.

## Homologação

O primeiro lote deve ser pequeno. Nada disso publica anúncio nem envia foto para a Nuvemshop.

```bash
python -m wago discover
python -m wago scrape --limit 3 --lote 5
python -m wago fill-nuvemshop "data/out/wago lote 5 <data> master.csv" -o data/out/homologacao-nuvemshop.csv
python -m wago images "data/out/wago lote 5 <data> master.csv" --limit 3
python -m wago fill-ml "data/out/wago lote 5 <data> master.csv" --sheet Outros -o data/out/homologacao-ml.xlsx
```

`photos`, `ml-map` e `ml-templates` ficam de fora: usam API externa e alterariam conta ou catálogo.

## Testes

```bash
.venv/Scripts/pytest -q
```

A suíte usa fixtures sintéticas. Não faz login na Wago, não baixa o sitemap e não publica em Nuvemshop nem Mercado Livre.

`tests/test_fase4_integrado.py` percorre o miolo do pipeline num só cenário: grava um checkpoint, recupera o lote, gera o CSV de 31 colunas, baixa imagens com HTTP simulado e preenche duas abas do XLSX. Cada arquivo é fechado e aberto de novo antes da conferência. O mesmo módulo sobe o painel em `127.0.0.1`, recusa porta inválida, porta ocupada, parâmetro sem confirmação, aba inexistente e caminho fora de `data/out`, impede a segunda execução e, depois do `shutdown`, confirma que a porta não responde e que o histórico continua no SQLite.
