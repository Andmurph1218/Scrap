"""Estado persistente por SKU.

A fonte da verdade é o catálogo SQLite (`data/state/catalogo.sqlite`).
`skus_processados.txt` e `skus_sem_preco.txt` continuam sendo gravados como
espelho legível; na primeira abertura do catálogo eles são importados.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from wago import config
from wago.catalogo import (
    FILTRADO_SEM_CUSTO,
    POS_SCRAPE,
    Catalogo,
    chave_pasta,
)
from wago.config import LINKS_FILE, SKUS_PROCESSADOS, SKUS_SEM_PRECO, ensure_dirs

_IMAGENS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _ler_txt(caminho: Path) -> set[str]:
    if not caminho.exists():
        return set()
    return {
        line.strip().upper()
        for line in caminho.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _anexar_txt(caminho: Path, sku: str) -> None:
    ensure_dirs()
    with caminho.open("a", encoding="utf-8") as fh:
        fh.write(f"{sku.strip().upper()}\n")


def catalogo() -> Catalogo:
    cat = Catalogo(config.CATALOGO)
    if cat.meta("legado_importado") is None:
        _importar_legado(cat)
    return cat


def _importar_legado(cat: Catalogo) -> None:
    links: list[str] = []
    if LINKS_FILE.exists():
        try:
            dados = json.loads(LINKS_FILE.read_text(encoding="utf-8"))
            if isinstance(dados, list):
                links = [str(item) for item in dados]
        except (OSError, json.JSONDecodeError):
            links = []
    cat.registrar_descobertos(links)
    cat.enfileirar()
    for sku in _ler_txt(SKUS_PROCESSADOS):
        cat.marcar_scrape_ok(sku)
    for sku in _ler_txt(SKUS_SEM_PRECO):
        cat.marcar_sem_custo(sku)
    cat.gravar_meta("legado_importado", "1")
    sincronizar_saidas(cat)


def carregar_skus_processados() -> set[str]:
    return catalogo().skus(*POS_SCRAPE)


def registrar_sku_processado(sku: str, lote: int | None = None) -> None:
    catalogo().marcar_scrape_ok(sku, lote)
    _anexar_txt(SKUS_PROCESSADOS, sku)


def carregar_skus_sem_preco() -> set[str]:
    return catalogo().skus(FILTRADO_SEM_CUSTO)


def registrar_sku_sem_preco(sku: str) -> None:
    catalogo().marcar_sem_custo(sku)
    _anexar_txt(SKUS_SEM_PRECO, sku)


def registrar_falha_scrape(sku: str, motivo: str) -> None:
    if sku:
        catalogo().registrar_falha(sku, motivo)


def sincronizar_saidas(cat: Catalogo | None = None) -> None:
    """Atualiza SCRAPE_OK / AGUARDANDO_IMAGEM / EXPORTADO a partir de data/out."""
    cat = cat or catalogo()
    exportados: set[str] = set()
    csv_ns = config.OUT / "produtos-Nuvemshop-preenchido.csv"
    if csv_ns.exists():
        with csv_ns.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh, delimiter=";"):
                sku = str(row.get("SKU") or "").strip()
                if sku:
                    exportados.add(sku)
    pastas: set[str] = set()
    base = config.OUT / "imagens"
    if base.exists():
        for sub in base.iterdir():
            if sub.is_dir() and "__" in sub.name and any(
                arq.suffix.lower() in _IMAGENS for arq in sub.iterdir()
            ):
                pastas.add(chave_pasta(sub.name.split("__", 1)[0]))
    cat.reconciliar_saidas(exportados, pastas)


def limpar_estado_progresso() -> None:
    """Limpa o catálogo e os espelhos em texto para recomeçar do zero."""
    ensure_dirs()
    for arq in (SKUS_PROCESSADOS, SKUS_SEM_PRECO):
        if arq.exists():
            arq.unlink(missing_ok=True)
    cat = catalogo()
    cat.resetar()
