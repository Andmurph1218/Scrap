"""Validador de plano agnóstico: verifica capacidades do provider antes de executar."""

from __future__ import annotations

from typing import Any


FASES_ESPERADAS = (
    "criar",
    "renomear",
    "reparentar",
    "mover",
    "arquivar",
    "verificar",
    "excluir",
)


def validar_plano(plano: dict[str, Any], provider_nome: str) -> None:
    """Valida se o plano é compatível com o provider de destino.

    Args:
        plano: Dicionário do plano gerado por `gerar_plano` (contém chave "fases").
        provider_nome: Nome do provider ("nuvemshop" ou "bling").

    Raises:
        ValueError: se o plano não tiver estrutura esperada ou usar recurso não suportado.
    """
    # Validação de estrutura básica
    if not isinstance(plano, dict):
        raise ValueError("Plano deve ser um dicionário")

    fases = plano.get("fases")
    if not isinstance(fases, dict):
        raise ValueError("Plano deve conter 'fases' como dicionário")

    # Verifica se todas as fases esperadas existem
    fases_faltantes = [f for f in FASES_ESPERADAS if f not in fases]
    if fases_faltantes:
        raise ValueError(f"Plano incompleto: fases ausentes: {fases_faltantes}")

    # Verifica se cada fase é uma lista
    for fase_nome, acoes in fases.items():
        if not isinstance(acoes, list):
            raise ValueError(f"Fase {fase_nome!r} deve ser uma lista")

    # Regras específicas por provider
    if provider_nome == "bling":
        reparentar = fases.get("reparentar", [])
        if reparentar:
            raise ValueError(
                "Bling não suporta reparentar categorias pela API v3"
            )

    elif provider_nome == "nuvemshop":
        # Nuvemshop suporta todas as operações
        pass

    else:
        raise ValueError(f"Provider desconhecido: {provider_nome!r}")