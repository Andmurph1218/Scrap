"""
Etapa 1 (P0): gera mapa_geral_categorias_ml.json a partir da API ML.

Percorre a árvore de categorias do site MLB e marca folhas (eh_folha).
"""

from __future__ import annotations

import json
from typing import Any

from wago.config import MAP_FILE_GENERAL, ensure_dirs
from wago.ml.auth import api_call, refresh_ml_token


def _caminho_str(path_from_root: list[dict[str, Any]], nome: str) -> str:
    parts = [p.get("name", "") for p in path_from_root if p.get("name")]
    if not parts or parts[-1] != nome:
        parts.append(nome)
    return " > ".join(parts)


def _processar_no(
    cat_id: str,
    mapping: dict[str, dict[str, Any]],
    *,
    max_nodes: int | None = None,
) -> None:
    if max_nodes is not None and len(mapping) >= max_nodes:
        return
    if cat_id in mapping:
        return

    data = api_call(f"/categories/{cat_id}")
    if not data or not isinstance(data, dict):
        return

    nome = data.get("name", "")
    path_from_root = data.get("path_from_root") or []
    children = data.get("children_categories") or []
    eh_folha = len(children) == 0

    mapping[cat_id] = {
        "id": cat_id,
        "nome": nome,
        "caminho": _caminho_str(path_from_root, nome),
        "eh_folha": eh_folha,
        "parent_id": (path_from_root[-2]["id"] if len(path_from_root) >= 2 else None),
        "children_count": len(children),
    }

    if len(mapping) % 50 == 0:
        print(f"  … {len(mapping)} categorias mapeadas")
        _salvar(mapping)

    for child in children:
        child_id = child.get("id")
        if not child_id:
            continue
        if max_nodes is not None and len(mapping) >= max_nodes:
            return
        _processar_no(child_id, mapping, max_nodes=max_nodes)


def _salvar(mapping: dict[str, dict[str, Any]]) -> None:
    ensure_dirs()
    MAP_FILE_GENERAL.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def gerar_mapa_geral(
    *,
    site: str = "MLB",
    max_nodes: int | None = None,
    resume: bool = True,
) -> dict[str, dict[str, Any]]:
    """
    Constrói o mapa geral de categorias.

    Parameters
    ----------
    site:
        Código do site ML (padrão MLB = Brasil).
    max_nodes:
        Limite opcional para smoke tests.
    resume:
        Se True, continua a partir do JSON existente.
    """
    token = refresh_ml_token()
    if not token:
        raise SystemExit("Token ML inválido. Configure ML_* no .env.")

    ensure_dirs()
    mapping: dict[str, dict[str, Any]] = {}
    if resume and MAP_FILE_GENERAL.exists():
        try:
            mapping = json.loads(MAP_FILE_GENERAL.read_text(encoding="utf-8"))
            print(f"Retomando mapa existente: {len(mapping)} categorias")
        except Exception as exc:
            print(f"Não foi possível ler mapa existente: {exc}")
            mapping = {}

    roots = api_call(f"/sites/{site}/categories")
    if not roots or not isinstance(roots, list):
        raise SystemExit(f"Falha ao listar categorias raiz de {site}.")

    print(f"Raízes em {site}: {len(roots)}")
    for root in roots:
        root_id = root.get("id")
        if not root_id:
            continue
        if max_nodes is not None and len(mapping) >= max_nodes:
            break
        print(f"Processando raiz: {root_id} — {root.get('name')}")
        _processar_no(root_id, mapping, max_nodes=max_nodes)

    _salvar(mapping)
    folhas = sum(1 for v in mapping.values() if v.get("eh_folha"))
    print(
        f"Mapa geral salvo em {MAP_FILE_GENERAL}\n"
        f"  Total: {len(mapping)} | Folhas: {folhas}"
    )
    return mapping
