"""Vincula fotos do CSV master aos produtos já importados na Nuvemshop."""

from __future__ import annotations

import base64
import glob
import re
import time
from pathlib import Path

import pandas as pd
import requests

from wago.config import LATEST_NUVEMSHOP_CSV, OUT, env, require_env


def limpar_sku(sku: str) -> str:
    return re.sub(r"\s+", "", str(sku)).strip().upper()


def _headers() -> dict[str, str]:
    creds = require_env("NUVEMSHOP_STORE_ID", "NUVEMSHOP_ACCESS_TOKEN")
    user_agent = env(
        "NUVEMSHOP_USER_AGENT", "JHSHOPPING (contato@jhshopping.com.br)"
    )
    return {
        "Authentication": f"bearer {creds['NUVEMSHOP_ACCESS_TOKEN']}",
        "User-Agent": user_agent or "JHSHOPPING (contato@jhshopping.com.br)",
        "Content-Type": "application/json",
    }


def _store_id() -> str:
    return require_env("NUVEMSHOP_STORE_ID")["NUVEMSHOP_STORE_ID"]


def carregar_mapa_skus_completo(
    headers: dict[str, str], store_id: str
) -> dict[str, int]:
    print("Mapeando produtos da Nuvemshop (todas as páginas)...")
    mapa: dict[str, int] = {}
    page = 1

    while True:
        url = (
            f"https://api.nuvemshop.com.br/v1/{store_id}/products"
            f"?per_page=200&page={page}"
        )
        res = requests.get(url, headers=headers, timeout=30)
        if res.status_code != 200:
            print(f"Erro ao buscar página {page}: {res.status_code} {res.text[:200]}")
            break

        produtos = res.json()
        if not produtos:
            break

        for prod in produtos:
            prod_id = prod.get("id")
            for variant in prod.get("variants", []):
                sku_raw = variant.get("sku")
                if sku_raw:
                    mapa[limpar_sku(sku_raw)] = prod_id

        print(f"  Página {page}: {len(produtos)} produtos")
        page += 1
        time.sleep(0.3)

    print(f"Mapeamento concluído: {len(mapa)} SKUs.\n")
    return mapa


def produto_ja_tem_imagens(
    headers: dict[str, str], store_id: str, product_id: int
) -> bool:
    url = f"https://api.nuvemshop.com.br/v1/{store_id}/products/{product_id}"
    try:
        res = requests.get(url, headers=headers, timeout=30)
        if res.status_code != 200:
            return False
        images = res.json().get("images") or []
        return len(images) > 0
    except Exception:
        return False


def buscar_product_id_direto(
    headers: dict[str, str], store_id: str, sku: str
) -> int | None:
    sku_clean = limpar_sku(sku)
    url = f"https://api.nuvemshop.com.br/v1/{store_id}/products?q={sku_clean}"
    res = requests.get(url, headers=headers, timeout=30)
    if res.status_code == 200 and res.json():
        for prod in res.json():
            for var in prod.get("variants", []):
                if limpar_sku(var.get("sku", "")) == sku_clean:
                    return prod.get("id")
    return None


def enviar_foto(
    headers: dict[str, str],
    store_id: str,
    product_id: int,
    foto_url: str,
) -> bool:
    url_api = (
        f"https://api.nuvemshop.com.br/v1/{store_id}/products/"
        f"{product_id}/images"
    )
    try:
        resp_img = requests.get(foto_url, timeout=20)
        if resp_img.status_code != 200:
            print(f"  Foto indisponível ({resp_img.status_code}): {foto_url}")
            return False

        # Preferência: enviar URL direta (mais leve); fallback attachment base64
        payload_src = {"src": foto_url}
        r = requests.post(url_api, headers=headers, json=payload_src, timeout=60)
        if r.status_code in (200, 201):
            return True

        b64_str = base64.b64encode(resp_img.content).decode("utf-8")
        filename = foto_url.split("/")[-1].split("?")[0] or "produto.jpg"
        payload = {"attachment": b64_str, "filename": filename}
        r = requests.post(url_api, headers=headers, json=payload, timeout=120)
        return r.status_code in (200, 201)
    except Exception as exc:
        print(f"  Erro ao processar foto {foto_url}: {exc}")
        return False


def _resolver_csv(caminho_csv: str | Path | None) -> Path:
    if caminho_csv:
        path = Path(caminho_csv)
        if not path.exists():
            raise SystemExit(f"Arquivo não encontrado: {path}")
        return path

    if LATEST_NUVEMSHOP_CSV.exists():
        return LATEST_NUVEMSHOP_CSV

    arquivos = sorted(
        glob.glob(str(OUT / "wago_nuvemshop_lote_*.csv")),
        key=lambda p: Path(p).stat().st_mtime,
        reverse=True,
    )
    if not arquivos:
        raise SystemExit(
            "Nenhum CSV de lote encontrado. Rode `python -m wago scrape` antes."
        )
    return Path(arquivos[0])


def vincular_fotos_lote(caminho_csv: str | Path | None = None) -> None:
    # P0: fail-fast de credenciais antes de qualquer rede
    headers = _headers()
    store_id = _store_id()

    caminho = _resolver_csv(caminho_csv)
    mapa_skus = carregar_mapa_skus_completo(headers, store_id)
    df = pd.read_csv(caminho, sep=";")

    print(f"Iniciando vinculação de fotos: {caminho}\n")
    sucessos = 0
    erros = 0
    pulados = 0

    for _, row in df.iterrows():
        sku_original = str(row.get("SKU", "")).strip()
        try:
            fotos_raw = str(row.get("Fotos", "")).strip()
            if not sku_original or not fotos_raw or fotos_raw.lower() == "nan":
                continue

            sku_clean = limpar_sku(sku_original)
            product_id = mapa_skus.get(sku_clean) or buscar_product_id_direto(
                headers, store_id, sku_original
            )

            if not product_id:
                print(
                    f"SKU {sku_original} não localizado na Nuvemshop "
                    "(importe o CSV antes)."
                )
                erros += 1
                continue

            if produto_ja_tem_imagens(headers, store_id, product_id):
                print(
                    f"  SKU {sku_original} (ID {product_id}): "
                    "já possui imagens — pulando."
                )
                pulados += 1
                continue

            lista_fotos = [
                f.strip()
                for f in fotos_raw.replace("\n", ",").split(",")
                if f.strip()
            ]
            for foto_url in lista_fotos:
                if enviar_foto(headers, store_id, product_id, foto_url):
                    print(
                        f"  SKU {sku_original} (ID {product_id}): "
                        "foto vinculada."
                    )
                    sucessos += 1
                else:
                    erros += 1
        except Exception as exc:
            print(f"  Erro inesperado no SKU {sku_original}: {exc}")
            erros += 1

        time.sleep(0.3)

    print(
        f"\nFim da vinculação. Sucessos: {sucessos} | "
        f"Pulados: {pulados} | Falhas: {erros}"
    )
