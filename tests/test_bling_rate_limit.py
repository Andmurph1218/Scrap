import pytest

from wago.marketplaces.base import ErroRateLimit
from wago.marketplaces.rate_limit import RateLimiter


class Relogio:
    def __init__(self, agora=100.0):
        self.agora = agora

    def __call__(self):
        return self.agora


def _limiter(por_dia=120_000):
    relogio, dormidas = Relogio(), []
    limiter = RateLimiter(por_dia=por_dia, relogio=relogio, dormir=lambda s: dormidas.append(s))
    return limiter, relogio, dormidas


def test_espaca_no_minimo_de_034s_entre_requisicoes():
    limiter, _relogio, dormidas = _limiter()
    limiter.adquirir()
    limiter.adquirir()
    assert dormidas == [0.34]


def test_cota_diaria_esgotada_levanta_erro_rate_limit():
    limiter, _relogio, _dormidas = _limiter(por_dia=2)
    limiter.adquirir()
    limiter.adquirir()
    with pytest.raises(ErroRateLimit) as exc:
        limiter.adquirir()
    assert exc.value.status == 429


def test_janela_diaria_reinicia_apos_24h():
    limiter, relogio, _dormidas = _limiter(por_dia=2)
    limiter.adquirir()
    limiter.adquirir()
    relogio.agora += 24 * 60 * 60
    limiter.adquirir()
    assert limiter.requisicoes_dia == 1


def test_retry_after_valido_define_o_atraso():
    limiter, _relogio, dormidas = _limiter()
    assert limiter.aguardar_429({"Retry-After": "2"}, 1) == 2.0
    assert dormidas == [2.0]


def test_retry_after_invalido_cai_no_backoff_exponencial():
    limiter, _relogio, dormidas = _limiter()
    assert limiter.aguardar_429({"Retry-After": "amanhã"}, 0) == 0.34
    assert limiter.aguardar_429({}, 2) == 1.36
    assert dormidas == [0.34, 1.36]
