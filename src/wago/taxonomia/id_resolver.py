"""IDResolver: traduz IDs alvo da taxonomia v8 para IDs do provider (Nuvemshop/Bling)."""

from __future__ import annotations

from typing import Any


class IDResolver:
    """Resolve IDs da taxonomia v8 (`id_alvo`) para IDs do marketplace de destino.

    - Nuvemshop: usa `nuvemshop_id` do nó v8.
    - Bling: usa `bling_mapping` (id_alvo → bling_id).
    - Referências `novo:<id_alvo>`: resolvidas via `diario_refs`.
    """

    def __init__(
        self,
        provider_nome: str,
        taxonomia_v8: dict,
        bling_mapping: dict[str, int],
        diario_refs: dict[str, int],
    ) -> None:
        self.provider_nome = provider_nome
        self.taxonomia_v8 = taxonomia_v8
        self.bling_mapping = bling_mapping
        self.diario_refs = diario_refs

        # Índice rápido: id_alvo → nó
        self._nos_por_id_alvo: dict[str, dict] = {
            no["id_alvo"]: no for no in taxonomia_v8.get("nos", [])
        }

    def resolver_categoria(self, id_alvo: str) -> int:
        """Resolve um `id_alvo` para o ID numérico do provider.

        Args:
            id_alvo: ID alvo da taxonomia (ex: "cat1") ou referência "novo:cat3".

        Returns:
            ID numérico da categoria no provider (Nuvemshop ou Bling).

        Raises:
            ValueError: se o ID não puder ser resolvido para o provider atual.
        """
        # Referência a categoria criada nesta execução (diário)
        if id_alvo.startswith("novo:"):
            if id_alvo not in self.diario_refs:
                raise ValueError(
                    f"Referência {id_alvo!r} não encontrada no diário (diario_refs)"
                )
            return self.diario_refs[id_alvo]

        # Busca o nó na taxonomia
        no = self._nos_por_id_alvo.get(id_alvo)
        if no is None:
            raise ValueError(f"id_alvo {id_alvo!r} não existe na taxonomia v8")

        if self.provider_nome == "nuvemshop":
            nuvemshop_id = no.get("nuvemshop_id")
            if nuvemshop_id is None:
                raise ValueError(
                    f"Categoria {id_alvo!r} não possui nuvemshop_id na taxonomia"
                )
            return int(nuvemshop_id)

        if self.provider_nome == "bling":
            if id_alvo not in self.bling_mapping:
                raise ValueError(
                    f"id_alvo {id_alvo!r} não está no bling_mapping"
                )
            return int(self.bling_mapping[id_alvo])

        raise ValueError(f"Provider desconhecido: {self.provider_nome!r}")

    def resolver_acao(self, acao: dict) -> dict:
        """Traduz os campos de ID de categoria em uma ação usando `resolver_categoria`.

        Campos traduzidos (se presentes): `categoria`, `pai`, `para`.
        Listas em `de` e `para` também são resolvidas elemento a elemento.
        O campo `ref` (referência de saída para ações 'criar') NÃO é resolvido.
        """
        resultado = dict(acao)  # cópia rasa

        # Campos únicos que podem conter ID de categoria (exceto 'ref' que é saída)
        for campo in ("categoria", "pai", "para"):
            if campo in resultado:
                valor = resultado[campo]
                if isinstance(valor, str):
                    resultado[campo] = self.resolver_categoria(valor)
                elif isinstance(valor, list):
                    resultado[campo] = [
                        self.resolver_categoria(v) if isinstance(v, str) else v
                        for v in valor
                    ]

        # Campo `de` (lista de IDs de origem em mover_produto)
        if "de" in resultado and isinstance(resultado["de"], list):
            resultado["de"] = [
                self.resolver_categoria(v) if isinstance(v, str) else v
                for v in resultado["de"]
            ]

        return resultado