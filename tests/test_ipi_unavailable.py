"""Falha de consulta de IPI não gera preço. IPI medido como zero continua válido."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wago.extract import (  # noqa: E402
    ResultadoIpi,
    capturar_ipi_carrinho,
    extrair_produto,
    preco_com_ipi,
)


def test_preco_nao_calcula_quando_consulta_falha():
    assert preco_com_ipi(10.0, ResultadoIpi(ok=False, motivo="bearer ausente")) is None


def test_preco_com_ipi_zero_e_valido():
    assert preco_com_ipi(10.0, ResultadoIpi(ok=True, valor=0.0), divisor=0.6) == 16.67


def test_preco_com_ipi_positivo():
    assert preco_com_ipi(7.18, ResultadoIpi(ok=True, valor=0.7), divisor=0.6) == 13.13


def test_timeout_ao_adicionar_nao_vira_zero():
    page = MagicMock()
    page.request.post.side_effect = TimeoutError("timeout")
    with patch("wago.extract._obter_bearer", return_value="Bearer tok"), patch(
        "wago.extract.limpar_carrinho"
    ) as limpar:
        result = capturar_ipi_carrinho(page, "221-413")
    assert result.ok is False
    assert result.valor is None
    assert "adicionar" in result.motivo
    assert limpar.call_count == 2


def test_http_do_carrinho_nao_vira_zero():
    page = MagicMock()
    page.request.post.return_value = MagicMock(ok=True)
    page.request.get.return_value = MagicMock(ok=False, status=503)
    page.inner_text.side_effect = Exception("sem html")
    with patch("wago.extract._obter_bearer", return_value="Bearer tok"), patch(
        "wago.extract.limpar_carrinho"
    ):
        result = capturar_ipi_carrinho(page, "221-413")
    assert result.ok is False
    assert result.valor is None
    assert result.motivo == "carrinho HTTP 503"


def test_ipi_zero_na_api_e_sucesso():
    page = MagicMock()
    page.request.post.return_value = MagicMock(ok=True)
    cart = MagicMock(ok=True)
    cart.json.return_value = {
        "cart": {
            "entries": [
                {
                    "product": {"code": "221-413"},
                    "quantity": 1,
                    "taxes": [
                        {
                            "code": "BX23",
                            "label": "IPI 0%",
                            "value": {"value": 0},
                        }
                    ],
                }
            ]
        }
    }
    page.request.get.return_value = cart
    with patch("wago.extract._obter_bearer", return_value="Bearer tok"), patch(
        "wago.extract.limpar_carrinho"
    ):
        result = capturar_ipi_carrinho(page, "221-413")
    assert result.ok is True
    assert result.valor == 0.0


def _pagina_produto() -> MagicMock:
    page = MagicMock()
    page.content.return_value = "<html><body><h1>Conector</h1></body></html>"
    page.inner_text.return_value = ""
    return page


def test_extrair_deixa_sku_pendente_sem_ipi(monkeypatch):
    sem_preco = MagicMock()
    monkeypatch.setattr("wago.state.registrar_sku_sem_preco", sem_preco)
    monkeypatch.setattr("wago.extract.extrair_preco_custo", lambda *a, **k: 10.0)
    monkeypatch.setattr(
        "wago.extract.capturar_ipi_carrinho",
        lambda *a, **k: ResultadoIpi(ok=False, motivo="bearer ausente"),
    )
    prod = extrair_produto(_pagina_produto(), "https://www.wago.com/br/p/221-413")
    assert prod is None
    sem_preco.assert_not_called()


def test_extrair_aceita_ipi_zero(monkeypatch):
    monkeypatch.setattr("wago.extract.extrair_preco_custo", lambda *a, **k: 10.0)
    monkeypatch.setattr("wago.extract.margin_divisor", lambda: 0.6)
    monkeypatch.setattr(
        "wago.extract.capturar_ipi_carrinho",
        lambda *a, **k: ResultadoIpi(ok=True, valor=0.0, motivo="ipi igual a zero"),
    )
    prod = extrair_produto(_pagina_produto(), "https://www.wago.com/br/p/221-413")
    assert prod is not None
    assert prod["IPI"] == 0.0
    assert prod["Preço"] == 16.67
    assert prod["Custo"] == 10.0
