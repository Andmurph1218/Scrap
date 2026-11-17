"""Rate limiter síncrono do Bling (3 req/s e 120.000/dia por conta).

O limite é por processo: dois jobs simultâneos podem somar. O worker do
dashboard já serializa a fila, então a responsabilidade fica do operador.

Relógio e sleep são injetáveis para os testes rodarem sem espera real.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping

from wago.marketplaces.base import ErroRateLimit

#: Espaçamento mínimo entre requisições (1/3 s arredondado, na prática 0.34).
INTERVALO_MINIMO = 0.34

#: Janela da cota diária: 24 horas a partir da primeira requisição do processo.
JANELA_DIARIA = 24 * 60 * 60


class RateLimiter:
    """Espaça chamadas e conta a cota diária antes de cada requisição ao Bling."""

    def __init__(
        self,
        por_segundo: int = 3,
        por_dia: int = 120_000,
        *,
        relogio: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self.por_segundo = por_segundo
        self.por_dia = por_dia
        self.intervalo = max(1.0 / por_segundo, INTERVALO_MINIMO)
        self.relogio = relogio
        self.dormir = dormir
        self.requisicoes_dia = 0
        self._ultima: float | None = None
        self._inicio_dia: float | None = None

    def adquirir(self) -> None:
        """Espera o intervalo mínimo, valida a cota diária e conta a requisição.

        Levanta `ErroRateLimit` quando a cota diária acabou: não dá para
        esperar 24h dentro de um job, então o operador precisa agir.
        """
        agora = self.relogio()
        if self._inicio_dia is None or agora - self._inicio_dia >= JANELA_DIARIA:
            self._inicio_dia = agora
            self.requisicoes_dia = 0

        if self.requisicoes_dia >= self.por_dia:
            raise ErroRateLimit(429, "cota diária do Bling esgotada")

        if self._ultima is not None:
            espera = max(0.0, self.intervalo - (agora - self._ultima))
            if espera > 0:
                self.dormir(espera)

        self._ultima = agora
        self.requisicoes_dia += 1

    def aguardar_429(self, headers: Mapping[str, str], tentativa: int) -> float:
        """Dorme o atraso do HTTP 429 e devolve o valor usado.

        Respeita `Retry-After` quando ele é um número válido; caso contrário
        usa o backoff exponencial `0.34 * 2 ** tentativa`.
        """
        atraso = self._atraso_retry_after(headers)
        if atraso is None:
            atraso = INTERVALO_MINIMO * 2**tentativa
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
