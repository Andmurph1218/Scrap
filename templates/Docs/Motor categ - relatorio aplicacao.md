# Motor de Categorias — Relatório da aplicação (05/11/2026)

Somente contagens agregadas. Snapshots, diários e IDs da loja ficam em `data/taxonomia/` (fora do Git).

## Taxonomia aplicada
- Versão aprovada: v5 (183 categorias: árvore de 2 níveis, sem genéricas, nomes ≤ 40 caracteres).
- Classificação: 98,3% automática (regras + Wago + categoria atual + IA Gemini com cache); restante revisado no dashboard.

## Piloto (04/11) — 3 rodadas com reversão
- Validado: criar, mover, arquivar (`visibility: hidden`), verificar vazia + excluir, reparentar subcategoria com 139 produtos (handle/URL mantidos).
- Reversão conferida pela API: 6/6 OK na rodada final.
- Descobertas corrigidas no código: PUT de categoria apaga campos omitidos; idiomas vazios dão 422; resposta do PUT pode vir defasada; produto guarda folha + ancestrais.

## 1ª passada
| Ação | OK | Falhas | Puladas |
|---|---|---|---|
| Criar categorias | 48 | 0 | 0 |
| Renomear | 8 | 0 | 0 |
| Reparentar | 23 | 0 | 0 |
| Mover produtos | 6.389 | 0 | 0 |
| Arquivar produtos | 348 | 0 | 0 |
| Verificar vazia | 103 | 21 | 0 |
| Excluir categorias | 103 | 0 | 20 |
| **Total** | **7.022** | **21** | **20** |

Falhas de API (429/5xx): **0**. As 21 verificações bloquearam pais que ainda tinham filhas, porque todas as verificações rodaram antes das exclusões. Corrigido: agora a verificação acontece logo antes de cada exclusão, das filhas para os pais.

## 2ª passada (complementar)
| Ação | OK | Falhas | Puladas |
|---|---|---|---|
| Mover produtos (ancestrais antigos) | 2.591 | 0 | 0 |
| Arquivar (limpeza de categorias) | 2 | 0 | 0 |
| Verificar vazia | 15 | 6 | 0 |
| Excluir categorias | 15 | 0 | 5 |
| **Total** | **2.623** | **6** | **5** |

Ajuste manual registrado em diário: produto fora de uso (calefator) ocultado e a categoria "Outros" duplicada verificada vazia e excluída.

## Estado final validado via API
- 183 de 183 nós da taxonomia existem na loja, com nome e pai corretos.
- Restam **5 categorias antigas** ("Automação e Elétrica Industrial", "Segurança e EPI", "Fixação e Montagem", "Controle de Movimento e Mecânica", "Interruptores e Tomadas"). Os 2.594 vínculos de produtos a elas são **só ancestrais antigos**: 100% desses produtos já estão na categoria final aprovada. A Nuvemshop continua regravando esse avô antigo nos produtos, mesmo com a árvore já corrigida. Pela regra de segurança, a exclusão dessas categorias só acontece com autorização do operador.
