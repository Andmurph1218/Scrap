"""Scraper de página de produto da Gavazzi (requests + BeautifulSoup)."""

from __future__ import annotations

import random
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from wago.config import DATA

BASE = "https://www.gavazziautomation.com"
CACHE_HTML = DATA / "raw" / "gavazzi"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def nova_sessao() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    return s


def baixar_pagina(url: str, session: requests.Session | None = None,
                  tentativas: int = 3, timeout: int = 30) -> str:
    """Baixa o HTML com retry exponencial. Levanta a última exceção."""
    sessao = session or nova_sessao()
    ultimo: Exception | None = None
    for tentativa in range(1, tentativas + 1):
        try:
            resp = sessao.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.text
            ultimo = RuntimeError(f"HTTP {resp.status_code}")
        except requests.RequestException as exc:
            ultimo = exc
        time.sleep(2 ** tentativa + random.random())
    raise RuntimeError(f"Falha ao baixar {url}: {ultimo}")


def _meta(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", property=prop)
    return (tag.get("content") or "").strip() if tag else ""


def _descricao(soup: BeautifulSoup) -> str:
    """og:description, com fallback ao div.heading se for mais completo."""
    og = _meta(soup, "og:description")
    candidato = ""
    for div in soup.select("div.heading"):
        texto = div.get_text(" ", strip=True)
        if texto and not div.select_one("h1, h2, .title"):
            if len(texto) > len(candidato):
                candidato = texto
    return candidato if len(candidato) > len(og) else og


def _especificacoes(soup: BeautifulSoup) -> dict[str, str]:
    specs: dict[str, str] = {}
    for row in soup.select(".detail-row"):
        cols = row.select(".col-6")
        if len(cols) < 2:
            continue
        chave = cols[0].get_text(" ", strip=True)
        valor = cols[-1].get_text(" ", strip=True)
        if chave and valor:
            specs[chave] = valor
    return specs


def _imagens(soup: BeautifulSoup) -> list[str]:
    """URLs absolutas da seção de download 'Images'; fallback og:image."""
    urls: list[str] = []
    for doc in soup.select(".product-document"):
        titulo = doc.select_one(".title")
        if not titulo or "image" not in titulo.get_text(strip=True).lower():
            continue
        for a in doc.select("li.item a[href]"):
            href = urljoin(BASE, a["href"])
            label = a.get_text(strip=True).lower()
            if "high" in label:
                urls.insert(0, href)  # alta resolução primeiro
            else:
                urls.append(href)
    if not urls:
        og = _meta(soup, "og:image")
        if og:
            urls.append(urljoin(BASE, og))
    # dedup preservando ordem, sem .tif (TIF não é aceito pelos marketplaces)
    vistos: set[str] = set()
    finais = []
    for u in urls:
        if u not in vistos and not u.lower().endswith(".tif"):
            vistos.add(u)
            finais.append(u)
    return finais


def _codigo(soup: BeautifulSoup, url: str) -> str:
    og = _meta(soup, "og:title")
    if og:
        return og.strip().upper()
    m = re.search(r"/product/([^/?&#]+)", url)
    if m:
        return m.group(1).upper()
    h3 = soup.select_one(".product-details h3")
    return h3.get_text(strip=True).upper() if h3 else ""


def extrair_produto(html: str, url: str = "") -> dict:
    """Extrai {codigo, titulo, descricao, especificacoes, imagens, url} do HTML."""
    soup = BeautifulSoup(html, "lxml")
    codigo = _codigo(soup, url)
    descricao = _descricao(soup)
    return {
        "codigo": codigo,
        "titulo": codigo,
        "descricao": descricao,
        "especificacoes": _especificacoes(soup),
        "imagens": _imagens(soup),
        "url": url,
    }


def scrapear_produto(url: str, codigo: str = "", session: requests.Session | None = None,
                     usar_cache: bool = True) -> dict:
    """Baixa e extrai um produto, com cache do HTML em data/raw/gavazzi/."""
    cache = CACHE_HTML / f"{codigo or Path(url).name}.html"
    if usar_cache and cache.exists():
        html = cache.read_text(encoding="utf-8")
    else:
        html = baixar_pagina(url, session=session)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(html, encoding="utf-8")
        time.sleep(0.5 + random.random())
    return extrair_produto(html, url=url)
