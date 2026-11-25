"""Tradutor EN→PT-BR para produtos Gavazzi.

Estratégia: Gemini via `gerar_json` com JSON estrito; se a IA estiver
indisponível/sem cota, cai para `deep-translator` (Google Translate).
Traduções ficam em cache (`data/state/gavazzi_traducoes.json`).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from wago.config import STATE
from wago.taxonomia.ia import ErroIA, obter_provedor

CACHE_TRADUCOES = STATE / "gavazzi_traducoes.json"

PROMPT = """Você é um tradutor técnico especializado em automação industrial (sensores, relés, controladores).
Traduza do inglês para o português do Brasil o produto abaixo.
Regras:
- Preserve códigos de produto, unidades (mm, V, A, Hz, °C), siglas (IO-Link, NPN, PNP, IP67, N.O., N.C.).
- Use terminologia técnica usual no Brasil (ex: "flush mountable" → "embutível/não-embutível", "housing" → "invólucro").
- Traduza APENAS as chaves das especificações, mantendo os valores originais.
- Responda SOMENTE com JSON estrito neste formato:
{{"titulo_pt": "<título>", "descricao_pt": "<descrição>", "specs_pt": {{"<chave_en>": "<chave_pt>", ...}}}}

Produto:
título: {titulo}
descrição: {descricao}
especificações: {specs}"""


def _google_traduzir(texto: str) -> str:
    from deep_translator import GoogleTranslator

    if not texto or not texto.strip():
        return texto
    # Google Translate limita ~5000 chars por chamada
    if len(texto) > 4500:
        texto = texto[:4500]
    return GoogleTranslator(source="en", target="pt").translate(texto)


def _carregar_cache() -> dict[str, dict]:
    if CACHE_TRADUCOES.exists():
        try:
            return json.loads(CACHE_TRADUCOES.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _salvar_cache(cache: dict[str, dict]) -> None:
    CACHE_TRADUCOES.parent.mkdir(parents=True, exist_ok=True)
    CACHE_TRADUCOES.write_text(
        json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def _chave(produto: dict) -> str:
    base = json.dumps(
        [produto.get("titulo", ""), produto.get("descricao", ""),
         produto.get("especificacoes", {})],
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _via_google(produto: dict) -> dict:
    specs = produto.get("especificacoes") or {}
    specs_pt = {k: _google_traduzir(k) for k in specs}
    return {
        "titulo_pt": _google_traduzir(produto.get("titulo", "")),
        "descricao_pt": _google_traduzir(produto.get("descricao", "")),
        "specs_pt": specs_pt,
        "tradutor": "google",
    }


def _via_ia(produto: dict, provider: Any) -> dict:
    prompt = PROMPT.format(
        titulo=produto.get("titulo", ""),
        descricao=produto.get("descricao", ""),
        specs=json.dumps(produto.get("especificacoes") or {}, ensure_ascii=False),
    )
    dados = provider.gerar_json(prompt)
    specs_pt = dados.get("specs_pt")
    return {
        "titulo_pt": str(dados.get("titulo_pt") or produto.get("titulo", "")),
        "descricao_pt": str(dados.get("descricao_pt") or produto.get("descricao", "")),
        "specs_pt": specs_pt if isinstance(specs_pt, dict) else {},
        "tradutor": getattr(provider, "nome", "ia"),
    }


def traduzir(produto: dict, provider: Any | None = None,
             usar_cache: bool = True, sem_ia: bool = False) -> dict:
    """Devolve o produto + campos *_pt. Fallback Google Translate em falha de IA."""
    codigo = produto.get("codigo", "")
    cache = _carregar_cache() if usar_cache else {}
    chave = _chave(produto)

    if chave in cache:
        return {**produto, **cache[chave]}

    if provider is None and not sem_ia:
        provider = obter_provedor()

    if provider is not None:
        try:
            traducao = _via_ia(produto, provider)
        except ErroIA:
            traducao = _via_google(produto)
    else:
        traducao = _via_google(produto)

    if usar_cache:
        cache[chave] = traducao
        _salvar_cache(cache)

    return {**produto, "codigo": codigo, **traducao}
