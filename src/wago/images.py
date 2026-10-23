"""Etapa 4b: baixa imagens do scrape em pastas Nome+SKU."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import pandas as pd
import requests

from wago.config import OUT, ensure_dirs

IMAGENS_DIR = OUT / "imagens"


def _slug(texto: str, max_len: int = 60) -> str:
    texto = (
        unicodedata.normalize("NFD", str(texto))
        .encode("ascii", "ignore")
        .decode("utf-8")
    )
    texto = re.sub(r"[^\w\s-]", "", texto).strip().lower()
    texto = re.sub(r"[-\s]+", "-", texto).strip("-")
    return (texto or "produto")[:max_len]


def pasta_produto(sku: str, nome: str) -> str:
    """Nome da pasta: SKU__slug-do-nome."""
    sku_clean = re.sub(r"[^\w.-]+", "_", str(sku).strip()) or "SEM-SKU"
    return f"{sku_clean}__{_slug(nome)}"


def _urls_fotos(raw: str) -> list[str]:
    if not raw or str(raw).lower() in ("nan", "none"):
        return []
    parts = re.split(r"[\n,;]+", str(raw))
    urls = []
    for p in parts:
        u = p.strip()
        if u.startswith("http"):
            urls.append(u)
    # dedupe preservando ordem
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _ext_from_url(url: str, content_type: str | None) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return suffix
    if content_type:
        if "png" in content_type:
            return ".png"
        if "webp" in content_type:
            return ".webp"
        if "gif" in content_type:
            return ".gif"
    return ".jpg"


def baixar_imagem(url: str, dest: Path, timeout: int = 30) -> bool:
    try:
        res = requests.get(url, timeout=timeout, stream=True)
        if res.status_code != 200:
            print(f"  Falha {res.status_code}: {url}")
            return False
        ext = _ext_from_url(url, res.headers.get("Content-Type"))
        if dest.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            dest = dest.with_suffix(ext)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            for chunk in res.iter_content(8192):
                if chunk:
                    fh.write(chunk)
        return True
    except Exception as exc:
        print(f"  Erro {url}: {exc}")
        return False


def baixar_imagens_lote(
    master_csv: Path | str,
    *,
    output_dir: Path | str | None = None,
    limit: int | None = None,
) -> Path:
    """
    Para cada produto do master, cria pasta SKU__nome e baixa as fotos.
    """
    ensure_dirs()
    path = Path(master_csv)
    if not path.exists():
        raise SystemExit(f"Master não encontrado: {path}")

    base = Path(output_dir) if output_dir else IMAGENS_DIR
    base.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    ok_produtos = 0
    ok_imgs = 0
    falhas = 0

    records: Iterable[dict] = df.to_dict(orient="records")
    for idx, row in enumerate(records):
        if limit is not None and idx >= limit:
            break
        sku = str(row.get("SKU", "")).strip()
        nome = str(row.get("Nome", "")).strip() or sku
        urls = _urls_fotos(row.get("Fotos", ""))
        if not sku:
            continue
        pasta = base / pasta_produto(sku, nome)
        pasta.mkdir(parents=True, exist_ok=True)

        if not urls:
            print(f"  {sku}: sem URLs de foto")
            falhas += 1
            continue

        baixadas = 0
        for i, url in enumerate(urls, start=1):
            dest = pasta / f"{i:02d}"
            if baixar_imagem(url, dest):
                baixadas += 1
                ok_imgs += 1
            else:
                falhas += 1

        if baixadas:
            ok_produtos += 1
            print(f"  {pasta.name}: {baixadas} imagem(ns)")
        else:
            print(f"  {sku}: nenhuma imagem baixada")

    print(
        f"\nImagens: {ok_produtos} pastas com foto | "
        f"{ok_imgs} arquivos | falhas={falhas}"
    )
    print(f"Diretório: {base}")
    return base
