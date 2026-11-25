"""Vincula as fotos do `gavazzi-imagens.json` aos produtos Gavazzi na Nuvemshop.

Reusa a infraestrutura de `wago.photos` (credenciais, varredura de SKUs,
upload robusto e cache de vinculadas). Idempotente: produtos que já têm
imagens reais são ignorados, a menos que `force=True` (limpa e reenvia).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wago.config import OUT, env, load_env, require_env
from wago.photos import (
    _headers,
    _imagens_reais,
    _store_id,
    autenticar_nuvemshop_auto,
    buscar_product_id_direto,
    carregar_mapa_skus_completo,
    enviar_foto_robusta,
    limpar_imagens_produto,
    limpar_sku,
    obter_imagens_produto,
)
from wago.state import carregar_skus_foto_vinculada, registrar_foto_vinculada

IMAGENS_JSON = OUT / "gavazzi-imagens.json"


def carregar_mapa_imagens(caminho: Path | str | None = None) -> dict[str, list[str]]:
    """Lê {sku: [urls]} do gavazzi-imagens.json."""
    path = Path(caminho) if caminho else IMAGENS_JSON
    if not path.exists():
        raise SystemExit(
            f"{path.name} não encontrado em {path.parent}. "
            "Rode o scrap Gavazzi antes (ele gera o mapeamento de imagens)."
        )
    dados = json.loads(path.read_text(encoding="utf-8"))
    mapa: dict[str, list[str]] = {}
    for sku, urls in dados.items():
        limpas = [str(u).strip() for u in (urls or []) if str(u).strip()]
        if limpas:
            mapa[str(sku).strip()] = limpas
    return mapa


def vincular_fotos_gavazzi(
    caminho: Path | str | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
    timeout: int = 20,
    cancelado: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Vincula as fotos aos produtos Nuvemshop por SKU. Retorna o resumo."""
    load_env()
    origem = Path(caminho) if caminho else IMAGENS_JSON
    mapa = carregar_mapa_imagens(origem)
    if not mapa:
        raise SystemExit(f"{origem.name} não contém imagens para vincular.")

    if dry_run:
        total_fotos = sum(len(u) for u in mapa.values())
        print(
            f"Simulação Gavazzi Fotos: {len(mapa)} SKU(s) | "
            f"{total_fotos} foto(s) pronta(s) para vincular ({origem.name})."
        )
        return {"sucessos": 0, "ignorados": 0, "falhas": 0}

    if not env("NUVEMSHOP_STORE_ID") or not env("NUVEMSHOP_ACCESS_TOKEN"):
        if os.getenv("PYTEST_CURRENT_TEST"):
            require_env("NUVEMSHOP_STORE_ID", "NUVEMSHOP_ACCESS_TOKEN")
        print(
            "Credenciais NUVEMSHOP_STORE_ID / NUVEMSHOP_ACCESS_TOKEN ausentes no .env. "
            "Iniciando autorização automática no navegador..."
        )
        autenticar_nuvemshop_auto(cancelado=cancelado)

    headers = _headers()
    store_id = _store_id()

    skus_cache = set() if force else carregar_skus_foto_vinculada()
    mapa_skus = carregar_mapa_skus_completo(headers, store_id, cancelado=cancelado)

    total = len(mapa)
    sucessos = 0
    ignorados = 0
    falhas = 0

    for i, (sku_original, urls) in enumerate(mapa.items(), start=1):
        if cancelado and cancelado():
            print("Cancelamento solicitado durante a vinculação de fotos.")
            break
        sku_clean = limpar_sku(sku_original)
        if not force and sku_clean in skus_cache:
            print(f"  [{i}/{total}] {sku_original}: já vinculado — ignorado")
            ignorados += 1
            continue
        try:
            product_id = mapa_skus.get(sku_clean) or buscar_product_id_direto(
                headers, store_id, sku_original
            )
            if not product_id:
                print(
                    f"  [{i}/{total}] {sku_original}: não encontrado na Nuvemshop "
                    "(importe o CSV antes)"
                )
                falhas += 1
                continue

            imagens_atuais = _imagens_reais(
                obter_imagens_produto(headers, store_id, product_id)
            )
            if imagens_atuais and not force:
                registrar_foto_vinculada(sku_clean, product_id)
                print(
                    f"  [{i}/{total}] {sku_original}: já possui "
                    f"{len(imagens_atuais)} imagem(ns) — ignorado"
                )
                ignorados += 1
                continue
            if imagens_atuais and force:
                print(
                    f"  [{i}/{total}] {sku_original}: substituindo "
                    f"{len(imagens_atuais)} imagem(ns)…"
                )
                limpar_imagens_produto(headers, store_id, product_id, imagens_atuais)

            print(f"  [{i}/{total}] Vinculando {len(urls)} imagem(ns) de {sku_original}…")
            hashes: set[str] = set()
            ok = 0
            for url in urls:
                if cancelado and cancelado():
                    break
                if enviar_foto_robusta(
                    product_id, url, headers, store_id,
                    timeout=timeout, hashes_vistos=hashes,
                ):
                    ok += 1
            if ok:
                registrar_foto_vinculada(sku_clean, product_id)
                sucessos += 1
                print(f"  [{i}/{total}] {sku_original}: sucesso ({ok} foto(s))")
            else:
                falhas += 1
                print(f"  [{i}/{total}] {sku_original}: erro — nenhuma foto aceita")
        except Exception as exc:  # falha de um SKU não derruba o lote
            falhas += 1
            print(f"  [{i}/{total}] {sku_original}: erro — {exc}")

    resumo = {
        "origem": str(origem),
        "sucessos": sucessos,
        "ignorados": ignorados,
        "falhas": falhas,
    }
    resumo_path = OUT / "gavazzi-fotos-resumo.json"
    resumo_path.write_text(
        json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"Vinculação de fotos concluída: {sucessos} sucessos, "
        f"{ignorados} ignorados, {falhas} falhas."
    )
    return resumo
