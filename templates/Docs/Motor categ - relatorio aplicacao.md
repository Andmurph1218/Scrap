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

## Limpeza de categorias órfãs (06/11, autorizada pelo operador)

Sobraram 5 categorias fora da taxonomia cujos vínculos de produtos eram só ancestrais antigos regravados pela Nuvemshop — 100% desses produtos já estavam numa categoria final aprovada. Critério aplicado: excluir só se todos os produtos vinculados tiverem categoria final aprovada, com o estado anterior de cada vínculo salvo no diário para rollback.

- **Canary:** "Controle de Movimento e Mecânica" (1 vínculo) excluída primeiro. Verificado por GET: categoria removida, produto intacto nas categorias finais, API removeu a referência órfã sozinha.
- **Lote:** as outras 4 excluídas em seguida, uma por vez, com verificação just-in-time.

| Categoria | Vínculos órfãos | Resultado |
|---|---|---|
| Controle de Movimento e Mecânica | 1 | excluída (canary) |
| Automação e Elétrica Industrial | 1.448 | excluída |
| Interruptores e Tomadas | 854 | excluída |
| Fixação e Montagem | 284 | excluída |
| Segurança e EPI | 7 | excluída |

Ajuste cosmético no mesmo dia: espaços sobrando em 2 nomes de categoria (" Automação Industrial", "Materiais Elétricos ") corrigidos via PUT com preservação de campos.

## Estado final validado via API (06/11/2026)

- **183 categorias na loja = 183 nós aprovados.** Zero faltando, zero extras, zero divergência de nome ou de pai.
- 43 raízes, profundidade máxima de 2 níveis, sem genéricas.
- Árvore da loja 100% espelhada com `data/taxonomia/taxonomia.json`.
