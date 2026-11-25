"""Rate limiter síncrono do Bling (3 req/s e 120.000/dia por conta).

O limite é por processo: dois jobs simultâneos podem somar. O worker do
dashboard já serializa a fila, então a responsabilidade fica do operador.

Relógio e sleep são injetáveis para os testes rodarem sem espera real.
"""

from __future__ import annotations

import math
import time
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable, Mapping

from wago.marketplaces.base import ErroRateLimit

#: Espaçamento mínimo entre requisições (1/3 s arredondado, na prática 0.34).
INTERVALO_MINIMO = 0.34

#: Janela da cota diária: 24 horas a partir da primeira requisição do processo.
JANELA_DIARIA = 24 * 60 * 60


class CanceladoBling(Exception):
    pass


def esperar_cancelavel(segundos, cancelado=None, dormir=None):
    dormir = dormir or time.sleep
    restante = segundos
    while restante > 0:
        if cancelado and cancelado():
            raise CanceladoBling()
        intervalo = min(restante, 0.2)
        dormir(intervalo)
        restante -= intervalo
    if cancelado and cancelado():
        raise CanceladoBling()


def dia_bling():
    return datetime.now(timezone(timedelta(hours=-3))).date().isoformat()


def limiter_bling(auth=None):
    import hashlib
    from wago.config import STATE, env

    caminho = getattr(getattr(auth, "store", None), "caminho", STATE / "bling_tokens.json")
    conta = env("BLING_ACCOUNT_ID") or hashlib.sha256(str(Path(caminho).resolve()).encode()).hexdigest()
    return RateLimiter(banco=STATE / "bling.sqlite", conta=conta)


class RateLimiter:
    """Espaça chamadas e conta a cota diária antes de cada requisição ao Bling."""

    def __init__(
        self,
        por_segundo: int = 3,
        por_dia: int | None = None,
        *,
        banco: Path | None = None,
        conta: str = "principal",
        dia: Callable[[], str] = dia_bling,
        cancelado: Callable[[], bool] | None = None,
        relogio: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self.por_segundo = por_segundo
        self.por_dia = por_dia if por_dia is not None else 120_000
        self.banco = Path(banco) if banco else None
        self.conta = conta
        self.dia = dia
        self.cancelado = cancelado
        self.intervalo = max(1.0 / por_segundo, INTERVALO_MINIMO)
        self.relogio = relogio
        self.dormir = dormir
        self.requisicoes_dia = 0
        self._ultima: float | None = None
        self._inicio_dia: float | None = None
        if self.banco:
            self.banco.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.banco, timeout=30)) as con, con:
                con.execute("CREATE TABLE IF NOT EXISTS cota_config (conta TEXT PRIMARY KEY, limite INTEGER NOT NULL)")
                con.execute("CREATE TABLE IF NOT EXISTS cota_diaria (conta TEXT, dia TEXT, total INTEGER NOT NULL DEFAULT 0, leituras INTEGER NOT NULL DEFAULT 0, escritas INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0, bloqueada INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (conta, dia))")
                con.execute("INSERT OR IGNORE INTO cota_config VALUES (?, ?)", (self.conta, self.por_dia if por_dia is not None else 100_000))
            if por_dia is not None:
                self.definir_limite(por_dia)
            self.requisicoes_dia = self.consumo()["total"]

    def definir_limite(self, limite):
        if isinstance(limite, bool) or not isinstance(limite, int) or not 1 <= limite <= 120_000:
            raise ValueError("Limite diário deve ficar entre 1 e 120000")
        self.por_dia = limite
        if self.banco:
            with closing(sqlite3.connect(self.banco, timeout=30)) as con, con:
                con.execute("INSERT OR REPLACE INTO cota_config VALUES (?, ?)", (self.conta, limite))

    def consumo(self):
        if not self.banco:
            return {"dia": self.dia(), "total": self.requisicoes_dia, "leituras": 0, "escritas": 0, "retries": 0, "limite": self.por_dia, "bloqueada": False}
        with closing(sqlite3.connect(self.banco, timeout=30)) as con:
            linha = con.execute("SELECT total, leituras, escritas, retries, bloqueada FROM cota_diaria WHERE conta=? AND dia=?", (self.conta, self.dia())).fetchone()
            limite = con.execute("SELECT limite FROM cota_config WHERE conta=?", (self.conta,)).fetchone()[0]
        total, leituras, escritas, retries, bloqueada = linha or (0, 0, 0, 0, 0)
        return {"dia": self.dia(), "total": total, "leituras": leituras, "escritas": escritas, "retries": retries, "limite": limite, "bloqueada": bool(bloqueada)}

    def bloquear_dia(self):
        if self.banco:
            with closing(sqlite3.connect(self.banco, timeout=30)) as con, con:
                con.execute("INSERT OR IGNORE INTO cota_diaria (conta, dia) VALUES (?, ?)", (self.conta, self.dia()))
                con.execute("UPDATE cota_diaria SET bloqueada=1 WHERE conta=? AND dia=?", (self.conta, self.dia()))

    def _reservar(self, metodo, repeticao):
        with closing(sqlite3.connect(self.banco, timeout=30)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            dia = self.dia()
            con.execute("INSERT OR IGNORE INTO cota_diaria (conta, dia) VALUES (?, ?)", (self.conta, dia))
            total, bloqueada = con.execute("SELECT total, bloqueada FROM cota_diaria WHERE conta=? AND dia=?", (self.conta, dia)).fetchone()
            limite = con.execute("SELECT limite FROM cota_config WHERE conta=?", (self.conta,)).fetchone()[0]
            if bloqueada or total >= limite:
                raise ErroRateLimit(429, "cota diária do Bling esgotada")
            leitura = int(metodo.upper() in ("GET", "HEAD", "OPTIONS"))
            con.execute("UPDATE cota_diaria SET total=total+1, leituras=leituras+?, escritas=escritas+?, retries=retries+? WHERE conta=? AND dia=?", (leitura, 1 - leitura, int(repeticao), self.conta, dia))
            self.requisicoes_dia = total + 1

    def adquirir(self, *, metodo="GET", repeticao=False) -> None:
        """Espera o intervalo mínimo, valida a cota diária e conta a requisição.

        Levanta `ErroRateLimit` quando a cota diária acabou: não dá para
        esperar 24h dentro de um job, então o operador precisa agir.

        O instante registrado é lido depois do sleep, para a próxima chamada
        medir a lacuna a partir do momento real do envio (e não do início da
        espera), preservando os 3 req/s mesmo com latência de rede.
        """
        if self.cancelado and self.cancelado():
            raise CanceladoBling()
        agora = self.relogio()
        if not self.banco:
            if self._inicio_dia is None or agora - self._inicio_dia >= JANELA_DIARIA:
                self._inicio_dia = agora
                self.requisicoes_dia = 0
            if self.requisicoes_dia >= self.por_dia:
                raise ErroRateLimit(429, "cota diária do Bling esgotada")

        if self._ultima is not None:
            espera = max(0.0, self.intervalo - (agora - self._ultima))
            if espera > 0:
                if self.cancelado:
                    esperar_cancelavel(espera, self.cancelado, self.dormir)
                else:
                    self.dormir(espera)
                agora = self.relogio()

        self._ultima = agora
        if self.banco:
            self._reservar(metodo, repeticao)
        else:
            self.requisicoes_dia += 1

    def aguardar_429(self, headers: Mapping[str, str], tentativa: int) -> float:
        """Dorme o atraso do HTTP 429 e devolve o valor usado.

        Respeita `Retry-After` quando ele é um número válido; caso contrário
        usa o backoff exponencial `0.34 * 2 ** tentativa`.
        """
        atraso = self._atraso_retry_after(headers)
        if atraso is None:
            atraso = INTERVALO_MINIMO * 2**tentativa
        if self.cancelado:
            esperar_cancelavel(atraso, self.cancelado, self.dormir)
        else:
            self.dormir(atraso)
        return atraso

    @staticmethod
    def _atraso_retry_after(headers: Mapping[str, str]) -> float | None:
        valor = headers.get("Retry-After")
        if valor is None:
            return None
        try:
            atraso = float(valor)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(atraso) or atraso < 0:
            return None
        return atraso
