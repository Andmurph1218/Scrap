"""Coleta links de produtos Wago (sitemap)."""

from __future__ import annotations

import json
import random
import re

import requests

from wago.config import LINKS_FILE, SITEMAP_URL, USER_AGENTS, ensure_dirs


def _headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }


def sku_from_url(url: str) -> str:
    match = re.search(r"/p/([^/?#]+)", url)
    if match:
        return re.sub(r"\s+", "", match.group(1)).strip().upper()
    tail = url.rstrip("/").split("/")[-1].replace(".html", "")
    return re.sub(r"\s+", "", tail.split("?")[0]).strip().upper()


def mapear_links_site_wago(session: requests.Session | None = None) -> list[str]:
    """Baixa o sitemap de produtos e grava links_wago_coletados.json."""
    ensure_dirs()
    session = session or requests.Session()
    print("Arquivo de links ausente ou refresh pedido. Baixando sitemap Wago...")
    links: set[str] = set()

    try:
        res = session.get(SITEMAP_URL, headers=_headers(), timeout=60)
        res.raise_for_status()
        for href in re.findall(r"<loc>(.*?)</loc>", res.text):
            if "/p/" in href:
                links.add(href.strip())
    except Exception as exc:
        print(f"Erro ao baixar sitemap: {exc}")
        return []

    lista = sorted(links)
    LINKS_FILE.write_text(
        json.dumps(lista, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Mapeamento concluído: {len(lista)} links → {LINKS_FILE}")
    return lista


def carregar_links(force_refresh: bool = False) -> list[str]:
    if force_refresh or not LINKS_FILE.exists():
        return mapear_links_site_wago()
    data = json.loads(LINKS_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"Formato inválido em {LINKS_FILE}")
    return data
