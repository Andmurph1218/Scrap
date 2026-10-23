"""Configuração central: paths, env e constantes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Raiz do repositório (…/wago-catalog-processor)
ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RAW = DATA / "raw"
STATE = DATA / "state"
OUT = DATA / "out"
MAPS = DATA / "maps"

LINKS_FILE = RAW / "links_wago_coletados.json"
SKUS_PROCESSADOS = STATE / "skus_processados.txt"
SKUS_SEM_PRECO = STATE / "skus_sem_preco.txt"
AUTH_STATE = STATE / "wago_auth_state.json"
LATEST_NUVEMSHOP_CSV = OUT / "wago_nuvemshop_lote_latest.csv"

MAP_FILE_GENERAL = MAPS / "mapa_geral_categorias_ml.json"
MAP_FILE_TARGET = MAPS / "mapeamento_categorias_ml.json"
ML_TEMPLATES_DIR = OUT / "Planilhas de Categorias"

SITEMAP_URL = "https://www.wago.com/br/sitemaps/Product-pt_BR-BRL-br-wago.xml"
WAGO_HOME = "https://www.wago.com/br"
WAGO_LOGIN = "https://www.wago.com/br/login"
WAGO_CART = "https://www.wago.com/br/cart"

USER_AGENTS = [
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
]


def ensure_dirs() -> None:
    for path in (RAW, STATE, OUT, MAPS, ML_TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_env() -> None:
    load_dotenv(ROOT / ".env")
    ensure_dirs()


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is not None and isinstance(value, str):
        value = value.strip()
        if value == "":
            return default
    return value


def require_env(*names: str) -> dict[str, str]:
    """Fail-fast se alguma variável obrigatória estiver ausente."""
    missing = [n for n in names if not env(n)]
    if missing:
        joined = ", ".join(missing)
        print(
            f"❌ Variáveis obrigatórias ausentes no .env: {joined}\n"
            f"   Copie .env.example → .env e preencha.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return {n: env(n) for n in names}  # type: ignore[misc]


def margin_divisor() -> float:
    raw = env("MARGIN_DIVISOR", "0.6") or "0.6"
    try:
        value = float(raw)
    except ValueError as exc:
        raise SystemExit(f"❌ MARGIN_DIVISOR inválido: {raw}") from exc
    if value <= 0 or value > 1:
        raise SystemExit("❌ MARGIN_DIVISOR deve estar entre 0 e 1 (ex.: 0.6)")
    return value
