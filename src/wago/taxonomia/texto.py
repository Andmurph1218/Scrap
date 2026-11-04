"""Normalização de nomes e sanitização de texto de produtos."""

from __future__ import annotations

import html
import re
import unicodedata
import warnings

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

MAX_CHARS_TEXTO = 1500
_RE_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_RE_CONTROLE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\ufeff]")
_RE_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")


def sanitizar_texto(texto: str | None, limite: int = MAX_CHARS_TEXTO) -> str:
    """Remove HTML/tags/scripts/entidades/URLs/emojis antes de regras e IA (obrigatório na Fase 3)."""
    if not texto:
        return ""
    bruto = str(texto)
    if "<" in bruto and ">" in bruto:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
            sopa = BeautifulSoup(bruto, "lxml")
        for tag in sopa(["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()
        for br in sopa.find_all(["br", "p", "li", "tr", "div", "h1", "h2", "h3", "h4"]):
            br.insert_after(" ")
        bruto = sopa.get_text(" ")
    limpo = html.unescape(html.unescape(bruto))
    limpo = _RE_URL.sub(" ", limpo)
    limpo = _RE_EMOJI.sub(" ", limpo)
    limpo = _RE_CONTROLE.sub(" ", limpo).replace("\xa0", " ")
    limpo = re.sub(r"\s+", " ", limpo).strip()
    if len(limpo) > limite:
        limpo = limpo[:limite].rsplit(" ", 1)[0]
    return limpo


def sem_acentos(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )


def _singular(palavra: str) -> str:
    if len(palavra) <= 3:
        return palavra
    for sufixo, troca in (("oes", "ao"), ("aes", "ao"), ("ais", "al"), ("eis", "el"), ("res", "r"), ("zes", "z")):
        if palavra.endswith(sufixo):
            return palavra[: -len(sufixo)] + troca
    if palavra.endswith("s") and not palavra.endswith("ss"):
        return palavra[:-1]
    return palavra


def normalizar_nome(nome: str) -> str:
    """Chave de comparação: minúsculas, sem acento, sem pontuação, singular."""
    base = sem_acentos(str(nome or "")).lower()
    base = re.sub(r"[^a-z0-9]+", " ", base).strip()
    return " ".join(_singular(p) for p in base.split())
