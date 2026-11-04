"""Camada de IA agnóstica de provedor (Gemini padrão, OpenAI opcional) com controle de cota.

Quando o provedor informa limite de uso esgotado (HTTP 429 / RESOURCE_EXHAUSTED / insufficient_quota),
o operador é avisado e o processamento só continua quando o plano liberar novo uso.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import requests

from wago.config import env

GEMINI_MODELO_PADRAO = "gemini-flash-lite-latest"
OPENAI_MODELO_PADRAO = "gpt-4o-mini"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class ErroIA(Exception):
    pass


class CotaEsgotada(ErroIA):
    """Limite do plano atingido. `espera_s`: tempo informado pelo provedor (se houver)."""

    def __init__(self, provedor: str, mensagem: str, *, espera_s: float | None = None, diaria: bool = False):
        super().__init__(mensagem)
        self.provedor = provedor
        self.espera_s = espera_s
        self.diaria = diaria


class CanceladoIA(Exception):
    pass


class ProvedorIA(Protocol):
    nome: str
    modelo: str

    def gerar_json(self, prompt: str) -> dict: ...


@dataclass
class Uso:
    chamadas: int = 0
    tokens_entrada: int = 0
    tokens_saida: int = 0
    respostas_invalidas: int = 0
    pausas_cota: int = 0
    segundos_em_pausa: float = 0.0
    erros: list[str] = field(default_factory=list)


def _segundos(texto: str | None) -> float | None:
    if not texto:
        return None
    m = re.match(r"^\s*([\d.]+)\s*s?\s*$", str(texto))
    return float(m.group(1)) if m else None


def _json_da_resposta(texto: str) -> dict:
    texto = texto.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?|```$", "", texto, flags=re.MULTILINE).strip()
    dados = json.loads(texto)
    if isinstance(dados, list):
        dados = {"resultados": dados}
    if not isinstance(dados, dict):
        raise ValueError("resposta não é um objeto JSON")
    return dados


class _Base:
    nome = "base"

    def __init__(self, chave: str, modelo: str, *, timeout: int = 60, session: Any = None,
                 tentativas: int = 3, dormir: Callable[[float], None] = time.sleep) -> None:
        self.chave = chave
        self.modelo = modelo
        self.timeout = timeout
        self.session = session or requests.Session()
        self.tentativas = tentativas
        self.dormir = dormir
        self.uso = Uso()

    def _post(self, url: str, **kw: Any) -> Any:
        ultimo = ""
        for tentativa in range(1, self.tentativas + 1):
            try:
                resp = self.session.post(url, timeout=self.timeout, **kw)
            except requests.RequestException as exc:
                ultimo = f"falha de rede: {exc.__class__.__name__}"
                self.dormir(2 ** tentativa)
                continue
            if resp.status_code >= 500:
                ultimo = f"HTTP {resp.status_code}"
                self.dormir(2 ** tentativa)
                continue
            return resp
        raise ErroIA(f"{self.nome}: {ultimo}")


class GeminiProvider(_Base):
    nome = "gemini"

    def gerar_json(self, prompt: str) -> dict:
        resp = self._post(
            GEMINI_URL.format(modelo=self.modelo),
            headers={"x-goog-api-key": self.chave, "Content-Type": "application/json"},
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
            },
        )
        corpo = resp.json() if resp.content else {}
        if resp.status_code == 429:
            raise self._cota(corpo)
        if resp.status_code >= 400:
            msg = (corpo.get("error") or {}).get("message", f"HTTP {resp.status_code}")
            raise ErroIA(f"gemini: {msg[:300]}")
        self.uso.chamadas += 1
        meta = corpo.get("usageMetadata") or {}
        self.uso.tokens_entrada += int(meta.get("promptTokenCount") or 0)
        self.uso.tokens_saida += int(meta.get("candidatesTokenCount") or 0)
        try:
            texto = "".join(p.get("text", "") for p in corpo["candidates"][0]["content"]["parts"])
            return _json_da_resposta(texto)
        except (KeyError, IndexError, ValueError) as exc:
            self.uso.respostas_invalidas += 1
            raise ErroIA(f"gemini: resposta inválida ({exc.__class__.__name__})") from exc

    def _cota(self, corpo: dict) -> CotaEsgotada:
        erro = corpo.get("error") or {}
        espera, diaria = None, False
        for det in erro.get("details") or []:
            tipo = det.get("@type", "")
            if tipo.endswith("RetryInfo"):
                espera = _segundos(det.get("retryDelay"))
            if tipo.endswith("QuotaFailure"):
                for v in det.get("violations") or []:
                    if "day" in (v.get("quotaId", "") + v.get("quotaMetric", "")).lower():
                        diaria = True
        return CotaEsgotada("gemini", (erro.get("message") or "limite de uso atingido")[:300],
                            espera_s=espera, diaria=diaria)


class OpenAIProvider(_Base):
    nome = "openai"

    def gerar_json(self, prompt: str) -> dict:
        resp = self._post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {self.chave}", "Content-Type": "application/json"},
            json={"model": self.modelo, "temperature": 0, "response_format": {"type": "json_object"},
                  "messages": [{"role": "user", "content": prompt}]},
        )
        corpo = resp.json() if resp.content else {}
        if resp.status_code == 429:
            erro = corpo.get("error") or {}
            sem_saldo = erro.get("code") == "insufficient_quota"
            espera = _segundos(resp.headers.get("retry-after"))
            raise CotaEsgotada("openai", (erro.get("message") or "limite de uso atingido")[:300],
                               espera_s=espera, diaria=sem_saldo)
        if resp.status_code >= 400:
            raise ErroIA(f"openai: {(corpo.get('error') or {}).get('message', resp.status_code)}")
        self.uso.chamadas += 1
        uso = corpo.get("usage") or {}
        self.uso.tokens_entrada += int(uso.get("prompt_tokens") or 0)
        self.uso.tokens_saida += int(uso.get("completion_tokens") or 0)
        try:
            return _json_da_resposta(corpo["choices"][0]["message"]["content"])
        except (KeyError, IndexError, ValueError) as exc:
            self.uso.respostas_invalidas += 1
            raise ErroIA("openai: resposta inválida") from exc


def obter_provedor(nome: str | None = None, **kw: Any) -> ProvedorIA | None:
    """Lê o .env. Sem IA_PROVIDER, usa a primeira chave presente (Gemini → OpenAI)."""
    nome = (nome or env("IA_PROVIDER") or "").lower()
    timeout = int(env("IA_TIMEOUT", "60") or 60)
    if not nome:
        nome = "gemini" if env("GEMINI_API_KEY") else "openai" if env("OPENAI_API_KEY") else "nenhum"
    if nome == "gemini" and env("GEMINI_API_KEY"):
        return GeminiProvider(env("GEMINI_API_KEY"), env("IA_MODEL") or GEMINI_MODELO_PADRAO, timeout=timeout, **kw)
    if nome == "openai" and env("OPENAI_API_KEY"):
        return OpenAIProvider(env("OPENAI_API_KEY"), env("IA_MODEL") or OPENAI_MODELO_PADRAO, timeout=timeout, **kw)
    return None


# ---------------------------------------------------------------- controle de cota
def _proximo_reset_diario(agora: datetime) -> datetime:
    """Cotas diárias do Gemini renovam à meia-noite do horário do Pacífico."""
    try:
        from zoneinfo import ZoneInfo

        pacifico = ZoneInfo("America/Los_Angeles")
    except Exception:  # noqa: BLE001 - sem base de fusos: aproximação UTC-8
        pacifico = timezone(timedelta(hours=-8))
    local = agora.astimezone(pacifico)
    amanha = (local + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return amanha.astimezone(agora.tzinfo or timezone.utc)


@dataclass
class ControleCota:
    """Chama o provedor respeitando ritmo (IA_RPM) e pausando quando o plano esgota."""

    provedor: ProvedorIA
    log: Callable[[str], None] = print
    aviso: Callable[[dict], None] | None = None
    cancelado: Callable[[], bool] | None = None
    dormir: Callable[[float], None] = time.sleep
    agora: Callable[[], datetime] = lambda: datetime.now().astimezone()
    rpm: float = 10.0
    passo_espera: float = 15.0
    _ultima: float = 0.0
    uso_pausa: Uso = field(default_factory=Uso)

    def _aguardar(self, segundos: float) -> None:
        restante = segundos
        while restante > 0:
            if self.cancelado and self.cancelado():
                raise CanceladoIA("cancelado durante a espera pela cota da IA")
            passo = min(self.passo_espera, restante)
            self.dormir(passo)
            restante -= passo

    def chamar(self, prompt: str) -> dict:
        while True:
            if self.cancelado and self.cancelado():
                raise CanceladoIA("cancelado")
            intervalo = 60.0 / max(self.rpm, 0.1)
            decorrido = time.monotonic() - self._ultima
            if self._ultima and decorrido < intervalo:
                self._aguardar(intervalo - decorrido)
            self._ultima = time.monotonic()
            try:
                return self.provedor.gerar_json(prompt)
            except CotaEsgotada as exc:
                agora = self.agora()
                if exc.diaria:
                    retoma = _proximo_reset_diario(agora)
                    if exc.espera_s and exc.espera_s < 3600:
                        retoma = agora + timedelta(seconds=exc.espera_s)
                else:
                    retoma = agora + timedelta(seconds=max(exc.espera_s or 60.0, 5.0))
                espera = max((retoma - agora).total_seconds(), 5.0)
                info = {
                    "provedor": exc.provedor, "tipo": "diaria" if exc.diaria else "por_minuto",
                    "retoma_em": retoma.isoformat(timespec="seconds"), "espera_s": round(espera),
                    "mensagem": str(exc),
                }
                self.uso_pausa.pausas_cota += 1
                self.uso_pausa.segundos_em_pausa += espera
                self.log(
                    f"⚠ Limite de uso da IA ({exc.provedor}) atingido "
                    f"({'cota diária' if exc.diaria else 'limite por minuto'}). "
                    f"O processamento ficará pausado e continuará automaticamente somente quando o plano "
                    f"liberar novo uso, previsto para {retoma.strftime('%d/%m %H:%M')} "
                    f"(~{max(round(espera / 60), 1)} min). O progresso já classificado está salvo; "
                    "você pode cancelar e retomar depois sem perder nada."
                )
                if self.aviso:
                    self.aviso(info)
                self._aguardar(espera)
                self.log(f"Retomando o uso da IA ({exc.provedor})...")
