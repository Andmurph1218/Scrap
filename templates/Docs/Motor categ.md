# Motor de Categorias — Gerenciador de Taxonomia e Catálogo Universal (PIM)

## 1. Objetivo

Criar um motor local que:

1. Lê **todas as categorias e todos os produtos** da loja Nuvemshop (não só os vindos da Wago).
2. Define uma **taxonomia-alvo** (árvore de categorias correta e enxuta).
3. Classifica cada produto na categoria correta (regras determinísticas primeiro, IA como apoio, humano na dúvida).
4. Gera um **plano de mudanças (diff)** revisável: categorias a criar, produtos a mover, categorias a excluir.
5. Aplica o plano na Nuvemshop **somente após aprovação do operador**, com backup e possibilidade de reverter.

Princípios (herdados do `Script_mestre.md`): etapas faseadas, testáveis e reversíveis; nada é alterado na loja sem ação explícita do operador; tudo registrado em log/SQLite; segredos nunca aparecem em logs.

## 2. Situação atual (ponto de partida)

- Já existe cliente Nuvemshop em `src/wago/photos.py` (`_headers()`, paginação `products?per_page=200`, token do app sob medida no `.env`).
- O scraper já extrai a **categoria principal em português** da Wago (`extract.py`, códigos de categoria da API Wago) — fonte forte para os produtos Wago.
- O dashboard (`src/wago/dashboard/`) já tem execuções, eventos, SQLite, cancelamento e confirmação — o motor reaproveita essa base.
- Não há biblioteca de IA no projeto hoje (`requirements.txt`). A camada de IA será agnóstica de provedor, com chamada direta de API como modo padrão (ver §6.1 e §8).

## 3. Conceitos e regras de negócio

| Termo | Significado |
|---|---|
| Taxonomia-alvo | Árvore oficial de categorias (arquivo versionado `data/taxonomia/taxonomia.json`) |
| Categoria excedente | Categoria da loja que não existe na taxonomia-alvo (duplicada, vazia, nome errado, fragmento de breadcrumb, SKU, inglês etc.) |
| Mapeamento | Associação produto → categoria-alvo, com origem (`regra`, `wago`, `ia`, `humano`) e confiança (0–1) |
| Plano | Lista ordenada de ações atômicas: `criar`, `renomear`, `reparentar`, `mover_produto`, `excluir`. **Não existe ação `mesclar` na API** — mesclagem é uma composição dessas ações (ver Fase 4) |
| Snapshot | Cópia JSON completa de categorias + vínculos produto/categoria antes de aplicar |

Regras:

- Ordem obrigatória de aplicação: **criar → mover produtos → excluir**. Nunca excluir uma categoria que ainda tenha produtos ou subcategorias no plano.
- **Confiança ≥ 0.85** → categorizado automático; abaixo → **Aguardando Revisão Humana**.
- Decisão humana sempre prevalece e é memorizada (não é sobrescrita por novas execuções da IA).
- Categoria vazia e fora da taxonomia → candidata a exclusão. Categoria vazia mas dentro da taxonomia → mantida.
- **Árvore de no máximo 2 níveis** (categoria-pai → subcategoria). **O produto fica vinculado somente à categoria final (folha)**, nunca duplicado na categoria-pai.
- **Escopo universal**: produtos vindos da Wago + todos os produtos já cadastrados na Nuvemshop.

## 4. Etapas de trabalho

### Fase 0 — Decisões e preparação
- Configurar `IA_PROVIDER`, `IA_MODEL` e a chave do provedor escolhido no `.env` (Gemini ou Claude como padrão recomendado).
- Definir se produtos ficam só na categoria folha ou também nas categorias-pai.
- Definir o limiar de confiança e se a exclusão exige produto zerado (padrão: sim).
- Entregável: seção de decisões preenchida neste documento.

### Fase 1 — Inventário (somente leitura)
- Módulo `src/wago/taxonomia/nuvemshop_api.py`: cliente com paginação, retry em 429/5xx e respeito ao rate limit da Nuvemshop.
  - `GET /categories` (todas as páginas) e `GET /products` (todas as páginas, campos `id`, `name`, `categories`, `variants.sku`, `description`, `handle`).
- Gravar snapshot em `data/taxonomia/snapshots/<data-hora>.json`.
- Relatório de diagnóstico: total de categorias, categorias vazias, duplicadas (nome normalizado igual), com nomes suspeitos (inglês, SKU, “Página inicial”, “produtos”, “…”), produtos sem categoria, produtos em várias categorias.
- CLI: `python -m wago taxonomia inventario`.
- Testes com respostas HTTP simuladas.

### Fase 2 — Taxonomia-alvo
- Gerar uma proposta inicial de `taxonomia.json` a partir de:
  1. Categorias principais em português da Wago (já extraídas no master).
  2. Categorias atuais da loja que têm produtos e nome válido.
  3. Agrupamento por similaridade (normalização: minúsculas, sem acento, singular/plural, sinônimos).
- Cada nó: `id_alvo`, `nome`, `pai`, `sinonimos[]`, `palavras_chave[]`, `nuvemshop_id` (quando já existir).
- A proposta é gravada como `data/taxonomia/taxonomia.proposta.json` com status `pendente_aprovacao`.
- **Gate de aprovação humana (obrigatório):**
  - A árvore proposta é apresentada no dashboard (tela "Taxonomia base") em formato de árvore de 2 níveis, com nº estimado de produtos por nó e origem de cada nó (Wago / loja / agrupamento).
  - O operador pode **renomear, mover entre pais, mesclar, excluir ou adicionar nós**, e então clicar em **[Aprovar taxonomia]**.
  - Somente após a aprovação o arquivo é promovido para `data/taxonomia/taxonomia.json` (com `versao`, `aprovado_por`, `aprovado_em`) e a versão anterior é arquivada em `data/taxonomia/historico/`.
  - **A Fase 3 é bloqueada** (CLI e botão do dashboard) enquanto não houver uma `taxonomia.json` aprovada; qualquer edição posterior volta o status para `pendente_aprovacao` e exige nova aprovação antes de reclassificar.
- Testes: validação de árvore (sem ciclos, nomes únicos por nível, pai existente, máx. 2 níveis) e bloqueio da Fase 3 sem aprovação.

### Fase 3 — Classificação de produtos
**Pré-requisito:** `taxonomia.json` aprovada no gate da Fase 2.

**Sanitização obrigatória (antes de regras e IA):** toda descrição/nome passa por `sanitizar_texto()` em `taxonomia/texto.py`:
- Remove tags HTML, `<script>`/`<style>` com conteúdo, comentários e atributos (via BeautifulSoup, já dependência do projeto).
- Decodifica entidades HTML (`&nbsp;`, `&amp;`, `&#39;` …), remove URLs, emojis e caracteres de controle.
- Normaliza espaços/quebras de linha e limita o tamanho (ex.: 1.500 caracteres) para controlar custo de tokens.
- O texto sanitizado é o **único** usado por regras, IA e no hash do cache; o texto original nunca é enviado à IA.
- Testes com descrições reais da Nuvemshop contendo HTML aninhado, tabelas e entidades.

Cascata por produto, parando na primeira com confiança suficiente:
1. **Decisão humana memorizada** (`data/taxonomia/decisoes.json` / SQLite) — confiança 1.0.
2. **Fonte Wago**: SKU presente no master → categoria Wago mapeada para a taxonomia — alta confiança.
3. **Regras**: palavras-chave/sinônimos no nome e descrição; categoria atual da loja já válida.
4. **IA** (provedor configurado no `.env`, via `ia.py`): prompt com nome, descrição e a lista fechada de categorias-alvo, em lotes; resposta em JSON validado por schema `{categoria, confianca, justificativa}`; a IA também pode **propor categoria nova** com justificativa (alimenta a Aba 2). Resposta fora do schema ou categoria inexistente → descartada e produto vai para revisão humana.
5. Sem resultado → **Aguardando Revisão Humana**.
- Cache por hash do conteúdo do produto para não reconsultar a IA sem necessidade.
- CLI: `python -m wago taxonomia classificar [--sem-ia]`.
- Testes de cada camada e da precedência.

### Fase 4 — Geração do plano (diff)
- Comparar estado atual (snapshot) × taxonomia-alvo × mapeamentos.
- Saída `data/taxonomia/plano-<data-hora>.json` + resumo legível:
  - Categorias a **criar** (com pai).
  - Categorias a **renomear** (`PUT /categories/{id}` com novo `name` — mantém ID e vínculos).
  - Grupos de **mesclagem** (duplicadas → uma só), já expandidos em ações atômicas.
  - Produtos a **mover** (de → para).
  - Categorias a **excluir** (excedentes, vazias após as movimentações).
- Plano é determinístico e idempotente: rodar duas vezes sem mudanças gera plano vazio.

#### Mesclagem simulada (a API da Nuvemshop não tem endpoint de merge)

Para cada grupo de duplicadas, o motor escolhe uma **categoria sobrevivente B** e trata as demais como **origens A**:

- Critério de escolha de B (nesta ordem): já mapeada na taxonomia-alvo → mais produtos → tem `handle`/SEO preenchido → menor ID (mais antiga).
- Se o nome final desejado for diferente do nome de B, gera um `renomear` em B (nunca recria B, para preservar ID, URL e SEO).

Expansão de **"mesclar A em B"** em ações atômicas, nesta ordem obrigatória:

1. `renomear B` (se necessário) — `PUT /categories/{B}`.
2. `reparentar` cada subcategoria de A para B — `PUT /categories/{filha}` com `parent = B` (evita que filhas sejam apagadas/órfãs junto com A; respeitando o limite de 2 níveis).
3. `mover_produto` de **todos** os produtos de A para B — `PUT /products/{id}` com `categories = [B]` (regra de categoria-folha única).
4. **Verificação**: `GET /products?category_id={A}` (todas as páginas) e `GET /categories/{A}` → precisa retornar **0 produtos e 0 subcategorias**.
5. `excluir A` — `DELETE /categories/{A}`, **somente** se a verificação do passo 4 passou.

Se qualquer passo falhar, o grupo para ali, A **não** é excluída e o grupo fica com status `mesclagem_incompleta` no SQLite para retomada.

Exemplo no `plano-<data-hora>.json`:

```json
{"grupo": "merge-012", "tipo": "mesclagem", "sobrevivente": 501, "origens": [733],
 "acoes": [
   {"seq": 1, "acao": "renomear",      "categoria": 501, "nome": "Bornes de Passagem"},
   {"seq": 2, "acao": "reparentar",    "categoria": 880, "de": 733, "para": 501},
   {"seq": 3, "acao": "mover_produto", "produto": 9911, "de": [733], "para": [501]},
   {"seq": 4, "acao": "verificar_vazia", "categoria": 733},
   {"seq": 5, "acao": "excluir",       "categoria": 733}
 ]}
```
- CLI: `python -m wago taxonomia plano`.

### Fase 5 — Aplicação na Nuvemshop (escrita, com confirmação)
- Antes de aplicar: snapshot obrigatório.
- **Mesclagem simulada (a API da Nuvemshop não suporta merge nativo):** para mesclar A em B, o motor **move todos os produtos de A para B** (`PUT /products/{id}`) e só **no final exclui A** (`DELETE /categories/{A}`), após confirmar que A está vazia — conforme a expansão em ações atômicas descrita na Fase 4.
- Execução em fases globais, nesta ordem fixa (nunca intercaladas):
  1. `POST /categories` — criar categorias novas (pais antes das filhas; o ID retornado é gravado no SQLite e substitui os IDs provisórios do plano).
  2. `PUT /categories/{id}` — renomear e reparentar (inclui os passos 1–2 das mesclagens).
  3. `PUT /products/{id}` com `categories = [id_folha]` — mover produtos (inclui o passo 3 das mesclagens).
  4. Verificação de vazio de cada categoria a excluir (`GET /products?category_id=` paginado + `GET /categories/{id}`).
  5. `DELETE /categories/{id}` — excluir apenas as categorias que passaram na verificação; filhas antes dos pais.
- **Mesclagem não é uma chamada de API**: `aplicar.py` não tem função `merge`; executa as ações atômicas expandidas na Fase 4 e só chega ao `DELETE` da origem A se todos os produtos e subcategorias de A já estiverem em B.
- Antes de cada exclusão, reconsultar a categoria na API e **pular** (não falhar a execução) se ainda tiver produtos ou filhas; a categoria fica marcada como `exclusao_bloqueada` no relatório.
- Resposta 404 em `DELETE` (já excluída) é tratada como sucesso idempotente; 422/erro de validação interrompe só aquele grupo.
- Modo `--dry-run` (padrão no CLI), aplicação por grupo (só criar / só mover / só excluir) e por lote.
- Cancelamento cooperativo entre requisições; retomada de onde parou (ações concluídas marcadas no SQLite).
- **Rollback**: `python -m wago taxonomia reverter <snapshot>` recria categorias excluídas e restaura vínculos. Como a categoria recriada recebe **ID novo** na Nuvemshop, o rollback mantém um mapa `id_antigo → id_novo` para religar produtos e filhas (a URL/handle original é reaplicada quando possível).
- Log detalhado por ação em `data/taxonomia/aplicacao-<data-hora>.json`.

### Fase 6 — HUD do operador (dashboard)
Nova seção “Motor de Categorias” no dashboard existente, reaproveitando execuções, eventos e cards. Ver §5.

**Execução assíncrona obrigatória:** todas as operações pesadas — **Inventário, Geração da taxonomia, Classificação, Geração do plano e Aplicação na Nuvemshop** — são registradas como operações do `DashboardService` (`OPERACOES`/`ACOES_REAIS`) e rodam na **fila de execução em background já existente** (thread de execução + SQLite de eventos), nunca dentro da requisição HTTP.
- O endpoint apenas enfileira e retorna o ID da execução imediatamente; a interface acompanha por polling de `/api/painel` e `/api/eventos` (mesmo mecanismo das etapas atuais).
- Respeita a regra existente de **uma execução por vez**, o botão **Cancelar** (cancelamento cooperativo entre lotes/requisições) e a retomada pelo SQLite.
- Operações leves (listar propostas, salvar decisão humana, editar nó da taxonomia) são síncronas e respondem em milissegundos.
- Progresso exibido nos cards: produtos lidos/classificados, chamadas de IA, ações aplicadas/total.

### Fase 7 — Integração com o pipeline existente
- `fill-nuvemshop` passa a usar a taxonomia-alvo para preencher a coluna `Categorias` (produtos novos já entram na categoria certa).
- Produtos importados posteriormente são detectados na próxima execução do inventário e caem na fila de classificação.

### Fase 8 — Testes, homologação e documentação
- Suíte com fixtures sintéticas (sem chamar Nuvemshop/IA reais).
- Homologação: inventário real (leitura) → plano → aplicar em **pequeno lote** (ex.: 1 categoria nova, 5 produtos movidos, 1 exclusão) → conferir no painel da Nuvemshop → testar rollback.
- Atualizar `README.md`.

## 5. HUD do operador (front end)

Cabeçalho da seção: card com data do último inventário, nº de categorias atuais × alvo, e botão **“Atualizar inventário”**.

### Aba 1: Saneamento (Exclusões)
Métrica: "X Categorias Vazias/Excedentes encontradas".

Ação: Botão "Revisar e Excluir". Abre modal listando as categorias, com botão "Excluir na Nuvemshop".

Detalhes de implementação:
- Lista com nome, motivo (vazia, duplicada, nome inválido, fora da taxonomia) e nº de produtos atuais.
- Categorias com produtos aparecem bloqueadas até que a Aba 3 mova os produtos.
- Seleção múltipla + confirmação explícita antes do `DELETE`.
- **Aviso de SEO obrigatório no modal**, em destaque (âmbar), antes do botão "Excluir na Nuvemshop":
  > ⚠ Atenção: ao excluir, a URL desta categoria (ex.: `/bornes-de-passagem/`) deixará de existir. Links externos, Google e anúncios que apontam para ela retornarão erro 404. Considere mesclar em outra categoria e criar um redirecionamento 301 no painel da Nuvemshop.
- O modal lista a URL/handle de cada categoria selecionada e exige marcar a caixa "Entendo que as URLs serão perdidas" para habilitar o botão.
- O mesmo aviso aparece na confirmação de mesclagens (a URL da categoria de origem A é perdida).

### Aba 2: Expansão (Novas Categorias)
Métrica: "Y Novas Categorias propostas pela IA".

Ação: Botão "Revisar Propostas". Abre modal mostrando: Nome Proposto, Justificativa da IA, e botões [Criar na Nuvemshop] / [Rejeitar].

Detalhes de implementação:
- Exibir também: categoria-pai sugerida, nº de produtos que iriam para ela e exemplos.
- Permitir editar nome/pai antes de criar.
- Proposta rejeitada fica memorizada e não volta a ser sugerida.

### Aba 3: Organização Universal
Métrica:

Total de Produtos (todas as fontes)

Categorizados (Auto)

Aguardando Revisão Humana

Ação: Botão "Executar Organização". Abre a tabela de produtos para arrastar e soltar (ou selecionar no dropdown) a categoria correta.

Detalhes de implementação:
- Tabela paginada com filtros (fonte, status, categoria atual, confiança) e busca por nome/SKU.
- Colunas: produto, SKU, categoria atual, categoria proposta, origem, confiança, justificativa.
- Ações em massa: aprovar sugestões selecionadas, definir categoria para seleção.
- Botão “Aplicar na Nuvemshop” mostra o resumo do plano (criar / mover / excluir) e pede confirmação.

## 6. Arquitetura proposta

```
src/wago/taxonomia/
  nuvemshop_api.py   # cliente categorias/produtos (leitura e escrita)
  inventario.py      # snapshot + diagnóstico
  modelo.py          # taxonomia-alvo, validação da árvore, normalização de nomes
  classificador.py   # cascata humano → wago → regras → IA
  ia.py              # interface agnóstica + fábrica de provedores (ver §6.1)
  ia_providers/      # gemini.py, openai.py, anthropic.py, devin_api.py, devin_assistido.py, nenhum.py
  texto.py           # sanitizar_texto(): remove HTML/tags/entidades antes de regras e IA
  plano.py           # diff estado atual × alvo
  aplicar.py         # execução ordenada, retomada, rollback
data/taxonomia/      # taxonomia.json, decisoes, snapshots, planos, logs
tests/test_taxonomia_*.py
```

- Dashboard: novas operações em `service.py` (`taxonomia-inventario`, `taxonomia-classificar`, `taxonomia-plano`, `taxonomia-aplicar`) e endpoints para listar/editar propostas e decisões.
- SQLite do dashboard ganha tabelas `tax_mapeamentos`, `tax_propostas`, `tax_acoes`.

### 6.1 Camada de IA agnóstica (`ia.py`)

Contrato único usado pelo classificador, independente de fornecedor:

```python
class ProvedorIA(Protocol):
    nome: str
    def classificar(self, produtos: list[ProdutoIA], categorias: list[CategoriaAlvo]) -> list[RespostaIA]: ...

# RespostaIA = {produto_id, categoria_id | None, nova_categoria: {nome, pai, justificativa} | None,
#               confianca: float, justificativa: str}
```

- `obter_provedor()` lê o `.env` e devolve a implementação; o classificador nunca importa SDK de fornecedor.
- **Provedores:**

| `IA_PROVIDER` | Uso | Implementação |
|---|---|---|
| `gemini` | **Padrão recomendado** (rápido, barato, saída JSON nativa) | HTTP direto via `requests` (sem SDK pesado) |
| `anthropic` | Alternativa padrão (Claude Haiku/Sonnet) | HTTP direto via `requests` |
| `openai` | Opcional | HTTP direto via `requests` |
| `devin_api` | **Fallback** (não principal) | Cria sessão via API do Devin (`POST https://api.devin.ai/v1/sessions`, `DEVIN_API_KEY`) com `structured_output`; aguarda e lê o JSON. Mais lento e consome ACUs — usar só quando o provedor direto falhar |
| `devin_assistido` | Fallback manual | Exporta `data/taxonomia/pendentes-ia.json`; importa `respostas-ia.json` quando o operador/Devin preencher |
| `nenhum` | Só regras + Wago + humano | Retorna vazio → produtos vão para revisão |

- **Chamadas diretas preparadas desde o dia 1:** `ia.py` já nasce com os provedores `gemini` (`GEMINI_API_KEY`) e `openai` (`OPENAI_API_KEY`) implementados e testados, por serem o caminho mais barato e rápido para automação contínua do dashboard. Se `IA_PROVIDER` não for definido, o motor escolhe automaticamente o primeiro com chave presente: `GEMINI_API_KEY` → `OPENAI_API_KEY` → `ANTHROPIC_API_KEY` → `nenhum`.

- **Variáveis no `.env`:**

```
IA_PROVIDER=gemini            # gemini | openai | anthropic | devin_api | devin_assistido | nenhum (vazio = auto)
IA_MODEL=                     # vazio = modelo default do provedor
IA_FALLBACK=devin_api         # provedor usado se o principal falhar
DEVIN_API_KEY=
IA_LOTE=25                    # produtos por chamada
IA_TIMEOUT=60
IA_MAX_PRODUTOS_POR_EXECUCAO=2000
GEMINI_API_KEY=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
```

- **Garantias de produção:**
  - Prompt único versionado (`taxonomia/prompts/classificar_v1.txt`), igual para todos os provedores; temperatura 0.
  - Saída sempre validada contra schema; `categoria_id` precisa existir na taxonomia-alvo; confiança é limitada a [0, 1].
  - Retry com backoff exponencial em 429/5xx/timeout; ao esgotar, cai para `IA_FALLBACK` e, por fim, para revisão humana.
  - Cache por hash (produto + versão do prompt + versão da taxonomia + provedor/modelo) em SQLite.
  - Registro por chamada: provedor, modelo, tokens/custo estimado, latência, nº de respostas inválidas — exibido no dashboard.
  - Chaves de API entram em `_CHAVES_SECRETAS` do dashboard (nunca aparecem em logs).
  - Testes com provedor falso (`FakeProvedor`) e respostas HTTP simuladas para cada provedor real.

## 7. Riscos e mitigações

| Risco | Mitigação |
|---|---|
| Excluir categoria com produtos / quebrar navegação da loja | Ordem criar→mover→excluir, reconsulta antes do `DELETE`, snapshot e rollback |
| Rate limit da API Nuvemshop | Fila com espaçamento, retry exponencial em 429 |
| IA classificar errado | Lista fechada de categorias, limiar de confiança, revisão humana, decisões memorizadas |
| Custo de IA | Cache por hash, regras/Wago antes da IA, lotes por chamada, teto `IA_MAX_PRODUTOS_POR_EXECUCAO`, modo `--sem-ia` |
| Provedor de IA fora do ar / limite / chave inválida | Retry com backoff; esgotado, os produtos vão para revisão humana ou para o fallback configurado (`IA_FALLBACK`); execução nunca trava |
| Dependência de fornecedor | Interface agnóstica: trocar de provedor = mudar `IA_PROVIDER` no `.env`, sem alterar código |
| Mudança de URLs/SEO de categorias | Preferir renomear a sobrevivente em vez de excluir e recriar; na mesclagem, escolher como sobrevivente a categoria com SEO/handle já estabelecido |
| Ausência de endpoint de merge na API | Mesclagem simulada (reparentar filhas → mover produtos → verificar vazio → excluir origem), com parada segura por grupo |
| Execução interrompida | Ações marcadas no SQLite, retomada idempotente |

## 7.1 Regras do cliente (edge cases reais) — implementadas

| Regra | Onde | Comportamento |
|---|---|---|
| Dicionário de tradução Wago | `modelo.TRADUCOES_WAGO` (Fases 2 e 3) | `Interface Modules and System Wiring` → `Módulos de Interface e Cabeamento`; `Energy Measurement Technology` → `Medição de Energia`; `Component Modules` → `Módulos de Componentes`. Aplicado às categorias da loja e da Wago; o nome original vira sinônimo e gera `renomear` na Fase 4. O prompt `prompts/classificar_v1.txt` instrui a IA a traduzir qualquer categoria em inglês. |
| Blacklist de genéricas | `modelo.CATEGORIAS_GENERICAS`, `classificador.aplicar_regras` | `Outros`, `Acessórios`, `Diversos`, `Geral` nunca recebem produto. Resposta da IA com elas → `Aguardando Revisão Humana`, confiança 0. O validador bloqueia aprovação se uma genérica for categoria final; como pai de subcategorias é permitida. |
| Eliminação de genéricas da árvore | `modelo.remover_genericas_finais` (Fase 2) | Nós genéricos sem subcategorias (ex.: `Acessórios`, 194 produtos) são removidos da taxonomia-alvo. Os produtos ficam com `reclassificar: true` no `mapa_categorias_loja` e são obrigatoriamente classificados pela descrição na Fase 3 (ex.: cabo → Cabos e Fios; parafuso → Elementos de Fixação). |
| Máx. 40 caracteres | `modelo.validar_taxonomia`, `modelo.ajustar_nome`, `classificador.ajustar_nova_categoria` | Validador bloqueia nomes > 40. Nomes longos conhecidos são resumidos (`modelo.ABREVIACOES`, ex.: `Bornes para PCI e Conectores`); sugestão da IA que continuar > 40 é recusada e vai para revisão. |
| Consolidação ≤ 2 produtos | `inventario.diagnosticar` (Fase 1) + alerta na proposta (Fase 2) | Lista `consolidacao_sugerida` com sugestão (excluir / mesclar no pai / mesclar em equivalente) para decisão no gate. |
| Árvores duplicadas | `modelo.CONSOLIDACOES_ARVORE` + `plano.grupos_mesclagem` (Fase 4) | `Materiais Elétricos` (sobrevivente B: mais subcategorias) ← `Comutação e Potência` (origem A). Plano: reparentar filhas de A → mover produtos de A → verificar A vazia → excluir A. Subcategorias homônimas nas duas árvores também são mescladas. |

## 8. Decisões fechadas (Fase 0)

| # | Tema | Decisão |
|---|---|---|
| 1 | IA | **`ia.py` agnóstico desde o dia 1** (detalhes em §6.1). **Modo padrão de produção: chamada de API direta** a um LLM rápido e barato, configurado no `.env` (`IA_PROVIDER=gemini` ou `anthropic`). `ia.py` já preparado para chamadas diretas via **`GEMINI_API_KEY`** ou **`OPENAI_API_KEY`** (mais barato e rápido para automação contínua do dashboard). **Fallback: API do Devin** (`devin_api`, `DEVIN_API_KEY`), mantida apenas para quando o provedor direto falhar; o modo manual "Assistido pelo Devin" (lote JSON) segue disponível como último recurso — nenhum dos dois é o caminho principal. **Lumi (Nuvemshop)** descartada: não tem API para chamada por código; o Admin MCP da Nuvemshop só expõe operações da loja, não um modelo de IA. |
| 2 | Vínculo do produto | **Só na categoria final (folha)**. O campo `categories` enviado no `PUT /products/{id}` terá apenas o ID da subcategoria final (ou da categoria de 1º nível quando ela não tiver filhas). |
| 3 | Profundidade da árvore | **Máximo de 2 níveis** (`Pai` → `Filha`). O validador de `taxonomia.json` rejeita nível 3+. |
| 4 | Limiar de confiança | **0.85**. Acima ou igual: categorizado automático. Abaixo: vai para **Aguardando Revisão Humana**. |
| 5 | Fontes de produtos | **Produtos Wago (master CSV) + todos os produtos já cadastrados na Nuvemshop**. |
| 6 | Lista inicial de categorias | **Não há lista prévia**. A Fase 2 vai gerar a proposta inicial automaticamente a partir das categorias em português da Wago e das categorias válidas atuais da loja, agrupadas em até 2 níveis para revisão do operador. |

