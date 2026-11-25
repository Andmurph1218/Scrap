import pytest

from wago.marketplaces.base import ErroRateLimit
from wago.marketplaces.rate_limit import RateLimiter


class Relogio:
    def __init__(self, agora=100.0):
        self.agora = agora

    def __call__(self):
        return self.agora


class RelogioAvancando:
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


def test_espacamento_se_sustenta_com_latencia_entre_requisicoes():
    relogio = RelogioAvancando()
    limiter = RateLimiter(relogio=relogio, dormir=lambda s: setattr(relogio, "agora", relogio.agora + s))
    inicios = []
    for _ in range(6):
        limiter.adquirir()
        inicios.append(relogio.agora)
        relogio.agora += 0.10  # latência simulada da requisição
    lacunas = [b - a for a, b in zip(inicios, inicios[1:])]
    assert all(gap >= 0.34 - 1e-9 for gap in lacunas), lacunas


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


def test_cota_persiste_entre_instancias_e_conta_leituras_escritas_retries(tmp_path):
    banco = tmp_path / "bling.sqlite"
    primeiro = RateLimiter(banco=banco, conta="loja", por_dia=3, dormir=lambda _: None)
    primeiro.adquirir(metodo="GET")
    segundo = RateLimiter(banco=banco, conta="loja", dormir=lambda _: None)
    assert segundo.requisicoes_dia == 1
    segundo.adquirir(metodo="PUT")
    primeiro.adquirir(metodo="PUT", repeticao=True)
    with pytest.raises(ErroRateLimit):
        segundo.adquirir()
    assert segundo.consumo() == {"dia": segundo.dia(), "total": 3, "leituras": 1, "escritas": 2, "retries": 1, "limite": 3, "bloqueada": False}


def test_cota_separa_contas_e_renova_no_dia_seguinte(tmp_path):
    hoje = ["2026-11-25"]
    kwargs = {"banco": tmp_path / "bling.sqlite", "dia": lambda: hoje[0], "dormir": lambda _: None}
    limiter = RateLimiter(conta="a", por_dia=1, **kwargs)
    limiter.adquirir()
    with pytest.raises(ErroRateLimit):
        limiter.adquirir()
    RateLimiter(conta="b", por_dia=1, **kwargs).adquirir()
    hoje[0] = "2026-11-26"
    limiter.adquirir()
    assert limiter.consumo()["total"] == 1


def test_limite_persistente_nao_e_resetado_por_novo_provider(tmp_path):
    banco = tmp_path / "bling.sqlite"
    RateLimiter(banco=banco, por_dia=100_000)
    assert RateLimiter(banco=banco).consumo()["limite"] == 100_000


def test_reservas_concorrentes_nao_ultrapassam_orcamento(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    banco = tmp_path / "bling.sqlite"
    RateLimiter(banco=banco, por_dia=5)

    def tentar(_):
        try:
            RateLimiter(banco=banco, dormir=lambda _: None).adquirir()
            return 1
        except ErroRateLimit:
            return 0

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(tentar, range(12))) == 5
    assert RateLimiter(banco=banco).consumo()["total"] == 5


def test_retry_after_valido_define_o_atraso():
    limiter, _relogio, dormidas = _limiter()
    assert limiter.aguardar_429({"Retry-After": "2"}, 1) == 2.0
    assert dormidas == [2.0]


def test_retry_after_invalido_cai_no_backoff_exponencial():
    limiter, _relogio, dormidas = _limiter()
    assert limiter.aguardar_429({"Retry-After": "amanhã"}, 0) == 0.34
    assert limiter.aguardar_429({}, 2) == 1.36
    assert dormidas == [0.34, 1.36]
