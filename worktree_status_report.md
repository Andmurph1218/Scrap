# Status do Worktree - marketplace-providers

## Resumo Final - Adaptacao Classificador para Bling v3

### Objetivo Concluído ✅
Adaptar classificador para Bling v3, rodar classificação, gerar plano-bling-real.json com 34.031 produtos sem categoria, e executar canary-bling-v2.json para validar PUT corrigido.

---

## Conclusões Importantes sobre a API Bling v3

### 1. Estrutura de Dados Bling
- **Campo categoria**: O Bling usa `categoria` (singular, dict) para um único item, ou `categorias` (array) para múltiplos
- **Campo situacao**: `situacao` (Ativo, Inativo, Excluido) em vez de `publicado`
- **Campo formato**: `formato` é obrigatório no PUT/POST ("S" = Simples)

### 2. Limite CRÍTICO da API Bling v3
**O PUT do Bling v3 NÃO permite remover categoria de um produto.**

Testes confirmados:
- `PUT /produtos/{id}` com `categorias: []` → categoria permanece
- `PUT /produtos/{id}` sem `categoria` no corpo → categoria permanece
- `Categoria: null` → ignorado

### 3. Solução Implementada
O `bling_provider.py` foi atualizado para:
- Tratar o campo `categorias` (array de objetos `{id}`)
- Mapear `publicado` → `situacao` ("Ativo"/"Inativo")
- Gerar o campo `formato` obrigatório

---

## Arquivos Chave

### Provider e Adapter
- `src/wago/marketplaces/bling_provider.py` - Provider Bling com atualizar_produto implementado
- `src/wago/taxonomia/bling_product_adapter.py` - Adapter para converter produtos Bling para formato canônico
- `src/wago/taxonomia/classificador.py` - Classificador adaptado para Bling

### Dados
- `data/state/bling_mapping_real.json` - Mapeamento ID real (239 entradas)
- `data/taxonomia/normalizados/snapshot-bling-normalizado-*.json` - Snapshot com categorias reais
- `data/taxonomia/planos/plano-bling-real.json` - Plano com 16.996 ações de atribuir

### Canary
- `canary-bling-v2.json` - Canary executado com sucesso (3 acoes)
- `data/taxonomia/diaries/canary-v2-*.json` - Diário de execução

---

## Execução do Canary

### Ações Testadas
1. **atribuir_categoria** ✅ - Atribui categoria a produto
   - Payload: `{"categorias": [{"id": 14852987}], "situacao": "A"}`
   - Resultado: Categoria adicionada com sucesso

2. **arquivar_produto** ✅ - Arquiva produto (situacao = "I")
   - Payload: `{"publicado": false}`
   - Resultado: Produto arquivado com sucesso

3. **rollback_arquivamento** ✅ - Desfaz o arquivamento
   - Payload: `{"publicado": true, "situacao": "A"}`
   - Resultado: Produto desarquivado com sucesso

### Limitação Documentada
O rollback DO PRODUTO SEM CATEGORIA não remove a categoria atribuída:
- `categoria_after_rollback: [14852987]` - categoria persiste!
- Esta é uma limitação documentada da API do Bling v3

---

## Estatísticas da Classificação

| Métrica | Valor |
|---------|-------|
| Produtos sem categoria (snapshot) | 34.031 |
| Produtos com categoria (snapshot) | 820 |
| Produtos auto-classificados | 16.996 |
| Produtos para revisão | 17.855 |
| Categorias a excluir | 244 |
| Categorias com produtos bloqueados | 36 |

---

## Próximos Passos

1. **Execute o script `scripts/executar_plano_bling.py`** para processar as atribuições
2. **Documente as categorias não removidas** no diário
3. **Para removão manual**: usar contato com suporte Bling ou endpoint específico se disponível

---

## Status Final

✅ **TODAS AS TAREFAS CONCLUÍDAS**

| Tarefa | Status |
|--------|--------|
| Adaptar classificador para Bling v3 | ✅ Concluído |
| Criar BlingProductAdapter | ✅ Concluído |
| Validar/reatribuir categorias (820 produtos) | ✅ Concluído |
| Rodar classificação sem IA | ✅ Concluído |
| Rodar classificação com IA | ✅ Concluído |
| Gerar plano-bling-real.json | ✅ Concluído |
| Criar canary-bling-v2.json | ✅ Concluído |
| Executar canary | ✅ Concluído (3/3 acoes OK) |