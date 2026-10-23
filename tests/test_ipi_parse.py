"""IPI do carrinho Wago BR: formato percentual + valor, sem falso positivo."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wago.extract import (  # noqa: E402
    _bloco_item_carrinho,
    _extrair_ipi_do_bloco,
    _extrair_spu,
    _parse_valor_ipi,
)


SAMPLE_LINE = (
    "Item n.º 221-413 Conector de emenda "
    "Seu preço por unidade R$ 7,18 PU (SPU): 500 peça "
    "Valor total sem IPI R$ 3.587,99 ICMS 18% (incluso) R$ 645,84 "
    "IPI 9.75% (à incluir) R$ 349,83 Remover "
    "Item n.º 279-327 Placa separadora"
)


def test_nao_pega_valor_total_sem_ipi():
    texto = "Valor total sem IPI R$ 1.590,08 ICMS 12% (incluso) R$ 190,81"
    assert _parse_valor_ipi(texto) is None


def test_parse_ipi_percentual_com_valor():
    texto = "IPI 9.75% (à incluir) R$ 349,83"
    assert _parse_valor_ipi(texto) == 349.83


def test_bloco_e_ipi_unitario_por_spu():
    bloco = _bloco_item_carrinho(SAMPLE_LINE, "221-413")
    assert bloco is not None
    assert "221-413" in bloco
    assert "279-327" not in bloco

    spu = _extrair_spu(bloco)
    assert spu == 500

    linha_ipi, aliq = _extrair_ipi_do_bloco(bloco)
    assert linha_ipi == 349.83
    assert aliq == 9.75
    assert round(linha_ipi / spu, 2) == 0.70
