"""Resolve código de produto Gavazzi → URL da página.

Estratégia: tenta a URL direta `/en-br/product/<CODIGO>` (o site responde 200
sem cHash quando o User-Agent é de navegador). Se 404, consulta o mapa do
sitemap (`data/state/gavazzi_links.json`).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from wago.config import STATE

BASE = "https://www.gavazziautomation.com"
SITEMAP_URL = f"{BASE}/en-br/sitemap.xml"
LINKS_JSON = STATE / "gavazzi_links.json"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _get(url: str, timeout: int = 30) -> requests.Response:
    return requests.get(url, timeout=timeout, headers={"User-Agent": _UA})


def url_direta(codigo: str) -> str:
    return f"{BASE}/en-br/product/{codigo}"


def _extrair_produtos_sitemap(xml_texto: str) -> dict[str, str]:
    """Extrai {CODIGO: url} de um <urlset> do sitemap."""
    mapa: dict[str, str] = {}
    for loc in re.findall(r"<loc>([^<]+)</loc>", xml_texto):
        loc = loc.replace("&amp;", "&")
        m = re.search(r"/product/([^/?&#]+)", loc)
        if m:
            mapa[m.group(1).upper()] = url_direta(m.group(1))
    return mapa


def baixar_mapa(destino: Path | None = None, timeout: int = 30) -> dict[str, str]:
    """Percorre o índice de sitemaps e monta {codigo: url} de todos os produtos."""
    destino = Path(destino) if destino else LINKS_JSON
    indice = _get(SITEMAP_URL, timeout=timeout)
    if indice.status_code != 200:
        raise RuntimeError(f"Sitemap indisponível: HTTP {indice.status_code}")

    raiz = ET.fromstring(indice.content)
    subs = [loc.text.strip() for loc in raiz.iter(f"{_NS}loc") if loc.text]
    sitemaps_produtos = [u for u in subs if "sitemap-type/products" in u]

    mapa: dict[str, str] = {}
    for url in sitemaps_produtos:
        try:
            resp = _get(url, timeout=timeout)
            if resp.status_code == 200:
                mapa.update(_extrair_produtos_sitemap(resp.text))
        except requests.RequestException:
            continue

    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(mapa, ensure_ascii=False, indent=1), encoding="utf-8")
    return mapa


def carregar_mapa(force_refresh: bool = False) -> dict[str, str]:
    if LINKS_JSON.exists() and not force_refresh:
        try:
            return json.loads(LINKS_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return baixar_mapa()


def resolver_url(codigo: str, mapa: dict[str, str], timeout: int = 30) -> str | None:
    """URL direta se a página existir; senão o mapa do sitemap; senão None."""
    url = url_direta(codigo)
    try:
        if _get(url, timeout=timeout).status_code == 200:
            return url
    except requests.RequestException:
        pass
    return mapa.get(codigo.upper())
