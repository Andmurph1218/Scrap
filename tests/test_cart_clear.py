"""capturar_ipi_carrinho: limpa antes/depois e lê IPI via API OCC."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wago.extract import capturar_ipi_carrinho, _ipi_unitario_da_entry  # noqa: E402


def test_ipi_unitario_da_entry():
    entry = {
        "quantity": 500,
        "basePrice": {"value": 7.18},
        "taxes": [
            {"code": "BX13", "label": "ICMS 18% (incluso)", "value": {"value": 645.84}},
            {
                "code": "BX23",
                "label": "IPI 9.75% (à incluir)",
                "value": {"value": 349.83},
            },
        ],
    }
    assert _ipi_unitario_da_entry(entry) == 0.70


def test_capturar_ipi_via_api_mockada():
    page = MagicMock()
    add_resp = MagicMock()
    add_resp.ok = True
    cart_resp = MagicMock()
    cart_resp.ok = True
    cart_resp.json.return_value = {
        "cart": {
            "entries": [
                {
                    "product": {"code": "221-413"},
                    "quantity": 500,
                    "basePrice": {"value": 7.18},
                    "taxes": [
                        {
                            "code": "BX23",
                            "label": "IPI 9.75% (à incluir)",
                            "value": {"value": 349.83},
                        }
                    ],
                }
            ]
        }
    }

    def request_side_effect(method, url, **kwargs):
        resp = MagicMock()
        resp.ok = True
        if "addall" in url:
            return add_resp
        if "fields=FULL" in url or url.endswith("current") or "carts/current?" in url:
            return cart_resp
        resp.json.return_value = {}
        return resp

    page.request.post.side_effect = lambda url, **kw: add_resp
    page.request.get.side_effect = lambda url, **kw: cart_resp
    page.request.delete.return_value = MagicMock(ok=True)

    with patch("wago.extract._obter_bearer", return_value="Bearer tok"), patch(
        "wago.extract.limpar_carrinho"
    ) as limpar:
        result = capturar_ipi_carrinho(page, "221-413")

    assert result.ok is True
    assert result.valor == 0.70
    assert limpar.call_count == 2


def test_capturar_ipi_limpa_mesmo_sem_bearer():
    page = MagicMock()
    with patch("wago.extract._obter_bearer", return_value=None), patch(
        "wago.extract.limpar_carrinho"
    ) as limpar:
        result = capturar_ipi_carrinho(page, "221-413")
    assert result.ok is False
    assert result.valor is None
    assert result.motivo == "bearer ausente"
    limpar.assert_not_called()


def test_capturar_ipi_sku_com_barra_vs_underscore():
    page = MagicMock()
    add_resp = MagicMock(ok=True)
    cart_resp = MagicMock(ok=True)
    cart_resp.json.return_value = {
        "cart": {
            "entries": [
                {
                    "product": {"code": "830-800/000-313"},
                    "quantity": 1,
                    "basePrice": {"value": 100.0},
                    "taxes": [
                        {
                            "code": "BX23",
                            "label": "IPI 9.75% (à incluir)",
                            "value": {"value": 9.75},
                        }
                    ],
                }
            ]
        }
    }
    page.request.post.return_value = add_resp
    page.request.get.return_value = cart_resp

    with patch("wago.extract._obter_bearer", return_value="Bearer tok"), patch(
        "wago.extract.limpar_carrinho"
    ) as limpar:
        result = capturar_ipi_carrinho(page, "830-800_000-313")

    assert result.ok is True
    assert result.valor == 9.75
    assert limpar.call_count == 2
