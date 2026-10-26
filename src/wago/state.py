"""Estado persistente: SKUs processados e blacklist sem preço."""

from __future__ import annotations

from wago.config import SKUS_PROCESSADOS, SKUS_SEM_PRECO, ensure_dirs


def carregar_skus_processados() -> set[str]:
    ensure_dirs()
    if not SKUS_PROCESSADOS.exists():
        return set()
    return {
        line.strip().upper()
        for line in SKUS_PROCESSADOS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def registrar_sku_processado(sku: str) -> None:
    ensure_dirs()
    with SKUS_PROCESSADOS.open("a", encoding="utf-8") as fh:
        fh.write(f"{sku.strip().upper()}\n")


def carregar_skus_sem_preco() -> set[str]:
    ensure_dirs()
    if not SKUS_SEM_PRECO.exists():
        return set()
    return {
        line.strip().upper()
        for line in SKUS_SEM_PRECO.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def registrar_sku_sem_preco(sku: str) -> None:
    ensure_dirs()
    with SKUS_SEM_PRECO.open("a", encoding="utf-8") as fh:
        fh.write(f"{sku.strip().upper()}\n")


def limpar_estado_progresso() -> None:
    """Limpa os contadores de SKUs processados e sem preço para recomeçar do zero."""
    ensure_dirs()
    for arq in (SKUS_PROCESSADOS, SKUS_SEM_PRECO):
        if arq.exists():
            arq.unlink(missing_ok=True)
