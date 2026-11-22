# Ledger — Escopo D: Bling v3 Taxonomy Sync

**Branch:** `feature/bling-taxonomy-sync` → `main` (ff-merge)
**Data:** 2026-11-21
**Status:** ✅ CONCLUÍDO — Merge final realizado em `main` (`36bb1b5`)

---

## Resumo Executivo

O Escopo D completou a sincronização completa da taxonomia Bling v3, processando **14.606 produtos com sucesso** em 34 lotes, tratando 230 erros (225 falhas permanentes HTTP 400 + 5 HTTP 429 transientes) e integrando os 2.393 produtos pendentes (2.163 ignorados por variação + 230 erros) ao Dashboard para revisão manual.

---

## T1 — Reprocessamento de 230 Erros ✅

**Script:** `scripts/reprocessar_erros_lotes.py`

| Resultado | Quantidade | Detalhe |
|-----------|------------|---------|
| **Recuperados (HTTP 429)** | 5 | Retry com sucesso após rate limit |
| **Falha permanente (HTTP 400)** | 225 | Adicionados à lista de exceções |

**Arquivo gerado:** `data/taxonomia/excecoes/lista_excecoes.json` (225 entradas `falha_permanente`)

> **Nota técnica:** A validação `Variedura sem variações` (formato=V) lança `ErroMarketplace(400,...)` em `bling_provider.py:459-468`. Esses 225 produtos são bloqueios definitivos da API Bling — não podem ser atualizados sem que o operador cadastre as variações no painel do Bling.

---

## T2 — Integração no Dashboard (Aba 3) ✅

**Arquivos modificados:**
- `src/wago/dashboard/taxonomia.py` — nova função `pendentes_bling()`
- `src/wago/dashboard/service.py` — ação `tax-atribuir-bling` + `_acao_tax_atribuir_bling()`
- `src/wago/dashboard/server.py` — rotas `GET /pendentes-bling` e `POST /acoes-bling`
- `src/wago/dashboard/static/index.html` — seletor provider, coluna motivo, avisos visuais

**Funcionalidades implementadas:**

| Feature | Descrição |
|---------|-----------|
| **Seletor Provider** | Dropdown "Marketplace: Bling / Taxonomia (Nuvemshop)" |
| **Listagem pendentes** | 2.393 produtos (2.163 ignorados + 230 erros) via `?source=bling` |
| **Categoria sugerida** | Preenchida automaticamente do `plano-bling-real.json` via `categoria_sugerida` |
| **Coluna Motivo** | Avisos visuais com tooltip: ⚠ "Revisão Manual: Variação" / ℹ "HTTP 400: ..." |
| **Mapeamento categoria** | `bling_mapping.json` (slug → bling_id) para conversão `id_alvo` → `bling_id` |
| **Chamada direta** | `BlingProvider.atualizar_produto()` via POST `/api/taxonomia/acoes-bling` |
| **Tratamento erros UI** | Toast amigável: variação → "Adicione variações no Bling" / outros → mensagem original |

**Endpoints novos:**
```
GET  /api/taxonomia/pendentes-bling          → lista 2.393 produtos com categoria_sugerida
POST /api/taxonomia/acoes-bling              → aplica categoria via BlingProvider
    Body: {ids: number[], id_alvo: string}
    Response: {sucesso, falhas, detalhes: [{produto, erro}]}
```

**Teste piloto (10 produtos):**
- 5 erros HTTP 400 → erro "Não foi possível atualizar o produto"
- 5 ignorados variação → erro "Não é possível atribuir categoria a produtos de Variedura sem variações: o Bling exige que variações sejam informadas para produtos com formato='V'"

---

## T3 — Verificação UTF-8 Encoding ✅

**Script:** `scripts/verificar_encoding.py` (executado com `PYTHONIOENCODING=utf-8`)

**Amostra:** 10 produtos aleatórios dos 14.606 com sucesso

**Resultado:** **VEREDICTO: OK** — Todos os 10 produtos mantêm acentos corretos na API Bling

**Exemplos verificados:**
- `fêmea` ✓ (16735895461)
- `espaço-econômico` ✓ (16735914907)
- `CARTUCHO DE PORCELANA gL-gG` ✓ (16565192989)
- `IND. M30 PLUG 50MM PNP` ✓ (16735898646)

**Arquivo:** `data/taxonomia/verificacao_encoding.txt`

> O encoding UTF-8 é nativo do `requests` (`json=corpo` em `bling_provider.py`). Nenhuma corrupção/mojibake detectada.

---

## Números Finais do Escopo D

| Métrica | Valor |
|---------|-------|
| **Lotes processados** | 34 |
| **Produtos com sucesso** | 14.606 |
| **Produtos ignorados (variação)** | 2.163 |
| **Produtos com erro HTTP 400** | 225 (falha permanente) |
| **Produtos com erro HTTP 429** | 5 (recuperados no retry) |
| **Total pendentes no Dashboard** | 2.393 |
| **Categorias no mapping Bling** | 165 (slug → bling_id) |
| **Ações no plano-bling-real.json** | 21.996 (fase mover) |

---

## Arquivos de Estado Gerados

```
data/taxonomia/
├── lotes/lote-001.json ... lote-034.json          # 34 lotes de execução
├── excecoes/lista_excecoes.json                   # 225 falhas permanentes
├── planos/plano-bling-real.json                   # Plano consolidado (21.996 mover)
├── state/bling_mapping.json                       # 165 categorias slug → bling_id
└── verificacao_encoding.txt                       # T3 veredito OK
```

---

## Próximos Passos (Pós-Merge)

1. **Operação manual no Dashboard:** Operador revisa 2.163 produtos "ignorados", cadastra variações no painel Bling e depois atribui categorias via Aba 3.
2. **Produtos erro (225):** Investigação caso a caso — restrições NF-e, marketplace, ou dados obrigatórios faltando.
3. **Monitoramento:** Novos lotes podem ser rodados conforme catálogo cresce.

---

## Checklist de Merge

- [x] T1: 230 erros reprocessados (5 OK, 225 exceções)
- [x] T2: Dashboard integrado (seletor, listagem, atribuição direta, avisos)
- [x] T3: Encoding UTF-8 verificado (OK)
- [x] Branch `feature/bling-taxonomy-sync` atualizada com commits T1+T2+T3
- [x] `ledger.md` documentado
- [x] **Merge ff-only para `main`** — `58d2a11..36bb1b5`, push `origin/main` OK
- [ ] Tag `bling-taxonomy-sync-v3` (opcional)

---

## Polimento Final de UX (pós-testes)

Refinamentos aplicados antes do merge, aprovados por teste manual no Dashboard:

| Ajuste | Resultado |
|--------|-----------|
| **Nomes legíveis nas sugestões** | A coluna "Sugestões" exibe `categoria_nome` do plano (ex.: "Tomadas") em vez do `bling_id` numérico |
| **Mensagem de erro limpa** | Removido prefixo duplicado `HTTP 400: HTTP 400: …` → `HTTP 400: Não foi possível atualizar o produto` |
| **Link rápido para o Bling** | Ícone 🔗 ao lado do ID abre `https://www.bling.com.br/produtos.php?id={id}` em nova aba |
| **Filtro de status funcional** | "Erros Bling" (230) e "Variação inválida" (2.163) filtram de verdade na lista |

**Suíte de testes:** 365 passed (regressões de `bling_provider` corrigidas; isolamento de `OUT` no `conftest.py` para testes de worker determinísticos).

---

**Autor:** Pipeline Scraping Operation — Bling v3 Taxonomy Sync
**Aprovação:** ✅ Teste manual final aprovado — Dashboard 100% funcional