"""Orquestra o scrape autenticado em lotes."""

from __future__ import annotations

import random
import time

from playwright.sync_api import sync_playwright

from wago.browser import preparar_navegador
from wago.config import require_env
from wago.discover import carregar_links, sku_from_url
from wago.export import (
    carregar_checkpoint_master,
    exportar_planilhas,
    gravar_checkpoint_master,
    salvar_csv_parcial,
    validar_registro_master,
)
from wago.extract import extrair_produto
from wago.state import (
    carregar_skus_processados,
    carregar_skus_sem_preco,
    registrar_sku_processado,
)


def recuperar_checkpoint(
    lote_numero: int, skus_validos: set[str]
) -> list[dict]:
    """
    Recarrega o master do lote já gravado.

    SKU recuperado entra na fila de processados para não ser scrapado de novo.
    Linha inválida fica de fora e pode ser tentada outra vez.
    """
    recuperados: list[dict] = []
    vistos: set[str] = set()
    for row in carregar_checkpoint_master(lote_numero):
        sku = str(row.get("SKU") or "").strip().upper()
        if not sku or sku in vistos:
            continue
        registro = dict(row)
        registro["SKU"] = sku
        motivo = validar_registro_master(registro)
        if motivo:
            print(f"  Checkpoint ignorado {sku}: {motivo}")
            continue
        recuperados.append(registro)
        vistos.add(sku)
        skus_validos.add(sku)
        if sku not in carregar_skus_processados():
            registrar_sku_processado(sku)
    if recuperados:
        print(
            f"Checkpoint do lote {lote_numero}: "
            f"{len(recuperados)} produto(s) recuperado(s)"
        )
    return recuperados


def incorporar_produto(
    produtos: list[dict],
    prod: dict,
    *,
    lote_numero: int,
    skus_validos: set[str],
) -> str | None:
    """
    Confirma o produto no lote.

    Devolve o motivo da rejeição, ou None quando o checkpoint foi gravado
    e o SKU foi registrado. Falha de gravação não registra o SKU.
    """
    sku = str(prod.get("SKU") or "").strip().upper()
    if sku and sku in skus_validos:
        return "SKU já processado"
    registro = dict(prod)
    registro["SKU"] = sku
    motivo = validar_registro_master(registro)
    if motivo:
        return motivo
    produtos.append(registro)
    try:
        gravar_checkpoint_master(produtos, lote_numero)
    except Exception:
        produtos.pop()
        raise
    try:
        registrar_sku_processado(sku)
    except Exception as exc:
        print(f"  SKU {sku} no checkpoint, mas o registro do SKU falhou: {exc}")
    skus_validos.add(sku)
    return None


def _dormir(segundos: float, cancelado) -> bool:
    """Espera em fatias curtas. Devolve False se o cancelamento foi pedido."""
    if cancelado is None:
        time.sleep(segundos)
        return True
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if cancelado():
            return False
        time.sleep(min(0.5, limite - time.monotonic()))
    return True


def rodar_scraper(
    *,
    limite_lote: int = 1000,
    lote_numero: int = 1,
    force_refresh_links: bool = False,
    headless: bool = True,
    cancelado=None,
) -> list[dict]:
    require_env("WAGO_USER", "WAGO_PASS")
    links_totais = carregar_links(force_refresh=force_refresh_links)
    if not links_totais:
        print("Nenhum link encontrado. Abortando.")
        return []

    skus_sem_preco = carregar_skus_sem_preco()
    skus_validos = carregar_skus_processados()
    produtos_lote = recuperar_checkpoint(lote_numero, skus_validos)

    links_para_rodar: list[str] = []
    for url in links_totais:
        sku_url = sku_from_url(url)
        if sku_url and sku_url not in skus_sem_preco and sku_url not in skus_validos:
            links_para_rodar.append(url)

    random.shuffle(links_para_rodar)
    print(
        f"Links totais: {len(links_totais)} | "
        f"pendentes: {len(links_para_rodar)} | "
        f"limite do lote: {limite_lote}"
    )

    coletados = 0

    with sync_playwright() as playwright:
        browser, context, page = preparar_navegador(
            playwright, headless=headless
        )
        try:
            for url in links_para_rodar:
                if cancelado and cancelado():
                    print(
                        "Cancelamento pedido. O que já foi gravado "
                        "no checkpoint segue para o master."
                    )
                    break
                if coletados >= limite_lote:
                    break

                prod = extrair_produto(page, url)
                if not prod:
                    continue

                sku = str(prod.get("SKU") or "").strip().upper()
                try:
                    motivo = incorporar_produto(
                        produtos_lote,
                        prod,
                        lote_numero=lote_numero,
                        skus_validos=skus_validos,
                    )
                except Exception as exc:
                    print(
                        f"  SKU {sku or '?'} não confirmado: "
                        f"falha ao gravar checkpoint ({exc})"
                    )
                    continue
                if motivo:
                    print(f"  SKU {sku or '?'} não confirmado: {motivo}")
                    continue

                coletados += 1

                print(
                    f"  [{coletados}/{limite_lote}] SKU {sku} | "
                    f"Custo: R${prod['Custo']:.2f} | "
                    f"Venda: R${prod['Preço']:.2f} | "
                    f"IPI: R${prod['IPI']:.2f}"
                )

                if coletados % 50 == 0:
                    salvar_csv_parcial(
                        produtos_lote,
                        f"wago_nuvemshop_checkpoint_{coletados}.csv",
                    )
                    print("Pausa anti-bloqueio (180s)...")
                    if not _dormir(180, cancelado):
                        print("Cancelamento pedido durante a pausa.")
                        break
                elif cancelado:
                    if not _dormir(random.randint(2500, 4500) / 1000, cancelado):
                        print("Cancelamento pedido.")
                        break
                else:
                    page.wait_for_timeout(random.randint(2500, 4500))
        finally:
            context.close()
            browser.close()

    if produtos_lote:
        exportar_planilhas(produtos_lote, lote_numero)
        if coletados == 0:
            print(
                "Nenhum produto novo. Master reexportado com "
                f"{len(produtos_lote)} produto(s) do checkpoint."
            )
    else:
        print("Nenhum produto novo coletado neste lote.")

    return produtos_lote
