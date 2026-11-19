"""Sincronização da taxonomia v8 para o Bling.

`BlingTaxonomySync` lê a árvore de categorias da v8
(`data/taxonomia/taxonomia.json`), carrega e grava o mapeamento de IDs locais
(`id` da v8) para `bling_id` em `data/state/bling_mapping.json`. A escrita do
mapeamento é atômica (`.tmp` + `os.replace`), sem criptografia, seguindo o
mesmo padrão do `TokenStore`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_ESTADO_PATH = Path("data/state/bling_mapping.json")
DEFAULT_TAXONOMIA_PATH = Path("data/taxonomia/taxonomia.json")


class BlingTaxonomySync:
    """Carregamento da taxonomia v8 e persistência do mapeamento de IDs."""

    def __init__(
        self,
        provider: Any = None,
        *,
        estado_path: Path | str = DEFAULT_ESTADO_PATH,
        taxonomia_path: Path | str = DEFAULT_TAXONOMIA_PATH,
    ) -> None:
        self.provider = provider
        self.estado_path = Path(estado_path)
        self.taxonomia_path = Path(taxonomia_path)

    def carregar_taxonomia(self, caminho: Path | str | None = None) -> list[dict]:
        """Lê a taxonomia v8 como lista de categorias.

        Aceita um arquivo com uma lista de categorias ou um dict com a chave
        `categorias`. Lança `FileNotFoundError` se o arquivo não existir.
        """
        p = Path(caminho) if caminho is not None else self.taxonomia_path
        if not p.exists():
            raise FileNotFoundError(f"Taxonomia não encontrada: {p}")
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return list(data.get("categorias", []))
        return list(data)

    def carregar_mapeamento(self) -> dict[str, int]:
        """Mapeamento `{id_v8: bling_id}` salvo, ou `{}` se ausente/corrompido.

        Arquivo inexistente, ilegível ou com JSON inválido vira `{}` vazio, para
        o sincronizador recomeçar de forma limpa sem interromper a execução.
        """
        if not self.estado_path.exists():
            return {}
        try:
            with open(self.estado_path, encoding="utf-8") as f:
                dados = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(dados, dict):
            return {}
        return dados

    def salvar_mapeamento(self, mapa: dict[str, int]) -> None:
        """Grava o mapeamento atomicamente, criando diretórios se necessário.

        A escrita usa `.tmp` + `os.replace`, sem sobra de arquivo temporário
        mesmo em caso de falha.
        """
        self.estado_path.parent.mkdir(parents=True, exist_ok=True)
        temporario = self.estado_path.with_suffix(".tmp")
        try:
            with open(temporario, "w", encoding="utf-8") as f:
                json.dump(mapa, f, ensure_ascii=False, indent=2)
            os.replace(temporario, self.estado_path)
        finally:
            try:
                temporario.unlink(missing_ok=True)
            except OSError:
                pass
