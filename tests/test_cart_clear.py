"""P0: capturar_ipi_carrinho deve limpar o carrinho ao final."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wago.extract import capturar_ipi_carrinho  # noqa: E402


def test_capturar_ipi_sempre_limpa_carrinho_apos_add():
    page = MagicMock()
    ok_resp = MagicMock()
    ok_resp.ok = True
    page.request.post.return_value = ok_resp

    cart_page = MagicMock()
    cart_page.content.return_value = (
        "<html><body>IPI Total R$ 1,50 Quantidade 1 unidade</body></html>"
    )
    page.context.new_page.return_value = cart_page

    with patch("wago.extract.obter_token_csrf", return_value="csrf"), patch(
        "wago.extract.limpar_carrinho"
    ) as limpar:
        result = capturar_ipi_carrinho(page, "221-413")

    assert result == 1.5
    limpar.assert_called_once_with(page)


def test_capturar_ipi_limpa_mesmo_se_leitura_falhar():
    page = MagicMock()
    ok_resp = MagicMock()
    ok_resp.ok = True
    page.request.post.return_value = ok_resp
    page.context.new_page.side_effect = RuntimeError("cart offline")

    with patch("wago.extract.obter_token_csrf", return_value=""), patch(
        "wago.extract.limpar_carrinho"
    ) as limpar:
        result = capturar_ipi_carrinho(page, "221-413")

    assert result == 0.0
    limpar.assert_called_once_with(page)
