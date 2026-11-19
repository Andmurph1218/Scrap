"""Migração de categorias antigas do Bling (CAIXA ALTA) para taxonomia v8.

Este módulo implementa a fase de migração específica do Bling:
- Lista categorias antigas (não presentes no bling_mapping)
- Match fuzzy: normaliza nomes (unidecode + lowercase + strip)
- Migra produtos das antigas para as novas v8
- Exclui categorias antigas que ficaram vazias (0 produtos + 0 subcategorias)
- Dry-run support
- Log explícito de não-encontradas
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from unidecode import unidecode

from wago.marketplaces.base import BaseMarketplaceProvider


def _normalizar(nome: str) -> str:
    """Normaliza nome para comparação fuzzy: unidecode + lowercase + strip."""
    return unidecode(nome).lower().strip()


def migrar_categorias_antigas_bling(
    provider: BaseMarketplaceProvider,
    taxonomia_v8: dict,
    bling_mapping: dict[str, int],
    log: Callable[[str], None] = print,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Migra produtos das categorias antigas do Bling (CAIXA ALTA) para as novas v8.

    Args:
        provider: Provider Bling (implementa BaseMarketplaceProvider)
        taxonomia_v8: Dict com chave "nos" contendo lista de categorias v8
                      Cada nó tem: id_alvo, nome, bling_id, ...
        bling_mapping: Dict {id_alvo: bling_id} de data/state/bling_mapping.json
        log: Função de log (padrão: print)
        dry_run: Se True, simula sem chamar escrita (atualizar_produto, deletar_categoria)

    Returns:
        Dict com:
            - "migradas": int (quantas categorias antigas tiveram produtos migrados)
            - "excluidas": int (quantas categorias antigas foram excluídas)
            - "nao_encontradas": list[str] (nomes das antigas sem match)
            - "erros": int (quantos erros ocorreram durante migração)
    """
    # 1. Carrega todas as categorias do provider
    categorias_provider = provider.listar_categorias()

    # 2. Identifica IDs das categorias NOVAS v8 (que estão no bling_mapping)
    novos_bling_ids = set(bling_mapping.values())

    # 3. Categorias ANTIGAS = todas que NÃO estão no bling_mapping
    categorias_antigas = [c for c in categorias_provider if c["id"] not in novos_bling_ids]

    # 4. Índice das novas v8 por nome normalizado (para match fuzzy)
    #    Só considera nós que têm bling_id
    novas_por_norm: dict[str, dict] = {}
    for no in taxonomia_v8.get("nos", []):
        bling_id = no.get("bling_id")
        if bling_id is not None:
            key = _normalizar(no["nome"])
            if key in novas_por_norm:
                # Match ambíguo: duas categorias v8 com mesmo nome normalizado
                # Marca como ambíguo para não fazer match automático
                novas_por_norm[key] = {"_ambiguo": True}
            else:
                novas_por_norm[key] = no

    # 5. Cache de produtos: listar_produtos() UMA vez
    todos_produtos = provider.listar_produtos()
    # Mapa: categoria_id -> lista de produtos nessa categoria
    produtos_por_categoria: dict[int, list[dict]] = defaultdict(list)
    for prod in todos_produtos:
        for cat_id in prod.get("categorias", []):
            produtos_por_categoria[cat_id].append(prod)

    # 6. Mapa de subcategorias: pai_id -> lista de filhos
    subcategorias_por_pai: dict[int, list[int]] = defaultdict(list)
    for cat in categorias_provider:
        pai_id = cat.get("pai_id")
        if pai_id is not None:
            subcategorias_por_pai[pai_id].append(cat["id"])

    # 7. Processa cada categoria antiga
    migradas = 0
    excluidas = 0
    nao_encontradas: list[str] = []
    erros = 0

    for antiga in categorias_antigas:
        id_antiga = antiga["id"]
        nome_antiga = antiga["nome"]
        key_antiga = _normalizar(nome_antiga)

        # Busca match nas novas v8
        nova_candidata = novas_por_norm.get(key_antiga)

        # Verifica se é match válido (existe e não é ambíguo)
        if nova_candidata is None or nova_candidata.get("_ambiguo"):
            log(f"[MIGRAÇÃO] SEM MATCH: '{nome_antiga}' (id={id_antiga}) — revisão manual necessária")
            nao_encontradas.append(nome_antiga)
            continue

        # Match encontrado
        id_nova = nova_candidata["bling_id"]
        nome_nova = nova_candidata["nome"]

        # Produtos na categoria antiga
        produtos_na_antiga = produtos_por_categoria.get(id_antiga, [])

        if not produtos_na_antiga:
            # Categoria antiga sem produtos - verifica se pode excluir direto
            tem_subcategorias = len(subcategorias_por_pai.get(id_antiga, [])) > 0
            if not tem_subcategorias:
                if not dry_run:
                    try:
                        provider.deletar_categoria(id_antiga)
                        excluidas += 1
                        log(f"  Antiga '{nome_antiga}' → excluída (já vazia)")
                    except Exception as exc:  # noqa: BLE001
                        erros += 1
                        log(f"  [ERRO] Falha ao excluir antiga '{nome_antiga}': {exc}")
                else:
                    log(f"  [DRY-RUN] Antiga '{nome_antiga}' seria excluída (já vazia)")
            continue

        # Migra produtos: para cada produto, adiciona nova_id e remove antiga_id
        for prod in produtos_na_antiga:
            pid = prod["id"]
            cats_atuais = list(prod.get("categorias", []))
            if id_nova not in cats_atuais:
                cats_atuais.append(id_nova)
            if id_antiga in cats_atuais:
                cats_atuais.remove(id_antiga)

            if not dry_run:
                try:
                    provider.atualizar_produto(pid, {"categorias": cats_atuais})
                    # Atualiza cache local
                    prod["categorias"] = cats_atuais
                except Exception as exc:  # noqa: BLE001
                    erros += 1
                    log(f"  [ERRO] Falha ao migrar produto {pid} de '{nome_antiga}' para '{nome_nova}': {exc}")
            else:
                log(f"  [DRY-RUN] Produto {pid} ('{prod['nome']}') seria movido: {cats_atuais}")

        migradas += 1
        log(f"  Antiga '{nome_antiga}' → {len(produtos_na_antiga)} produto(s) movidos para '{nome_nova}' (id={id_nova})")

        # Verifica se categoria antiga ficou vazia (0 produtos + 0 subcategorias)
        # Atualiza contagem de produtos na antiga (podem ter sobrado se erro)
        produtos_restantes = [p for p in todos_produtos if id_antiga in p.get("categorias", [])]
        tem_subcategorias = len(subcategorias_por_pai.get(id_antiga, [])) > 0

        if not produtos_restantes and not tem_subcategorias:
            if not dry_run:
                try:
                    provider.deletar_categoria(id_antiga)
                    excluidas += 1
                    log(f"  Antiga '{nome_antiga}' → excluída (vazia após migração)")
                except Exception as exc:  # noqa: BLE001
                    erros += 1
                    log(f"  [ERRO] Falha ao excluir antiga '{nome_antiga}': {exc}")
            else:
                log(f"  [DRY-RUN] Antiga '{nome_antiga}' seria excluída (vazia após migração)")
        else:
            if produtos_restantes:
                log(f"  [AVISO] Antiga '{nome_antiga}' ainda tem {len(produtos_restantes)} produto(s) — não excluída")
            if tem_subcategorias:
                log(f"  [AVISO] Antiga '{nome_antiga}' ainda tem subcategorias — não excluída")

    return {
        "migradas": migradas,
        "excluidas": excluidas,
        "nao_encontradas": nao_encontradas,
        "erros": erros,
    }