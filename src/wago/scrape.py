"""Orquestra o scrape autenticado em lotes."""

from __future__ import annotations

import random
import time

from playwright.sync_api import sync_playwright

from wago.browser import preparar_navegador
from wago.discover import carregar_links, sku_from_url
from wago.export import exportar_planilhas, salvar_csv_parcial
from wago.extract import extrair_produto
from wago.state import (
    carregar_skus_processados,
    carregar_skus_sem_preco,
    registrar_sku_processado,
)


def rodar_scraper(
    *,
    limite_lote: int = 1000,
    lote_numero: int = 1,
    force_refresh_links: bool = False,
    headless: bool = True,
) -> list[dict]:
    links_totais = carregar_links(force_refresh=force_refresh_links)
    if not links_totais:
        print("Nenhum link encontrado. Abortando.")
        return []

    skus_sem_preco = carregar_skus_sem_preco()
    skus_validos = carregar_skus_processados()

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

    produtos_lote: list[dict] = []
    coletados = 0

    with sync_playwright() as playwright:
        browser, context, page = preparar_navegador(
            playwright, headless=headless
        )
        try:
            for url in links_para_rodar:
                if coletados >= limite_lote:
                    break

                prod = extrair_produto(page, url)
                if not prod:
                    continue

                sku = prod["SKU"]
                if sku in skus_validos:
                    continue

                produtos_lote.append(prod)
                skus_validos.add(sku)
                registrar_sku_processado(sku)
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
                    time.sleep(180)
                else:
                    page.wait_for_timeout(random.randint(2500, 4500))
        finally:
            context.close()
            browser.close()

    if produtos_lote:
        exportar_planilhas(produtos_lote, lote_numero)
    else:
        print("Nenhum produto novo coletado neste lote.")

    return produtos_lote
