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
    """Carregamento da taxonomia v8, persistência e sincronização de IDs."""

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
        """Lê a taxonomia v8 como lista de categorias normalizadas.

        A taxonomia v8 usa a chave `nos` com objetos contendo:
        - `id_alvo` (string) → vira `id`
        - `nome` (string) → mantido
        - `pai` (string | None) → vira `pai_id`

        Aceita também o formato legado: lista direta ou dict com a chave
        `categorias`. Lança `FileNotFoundError` se o arquivo não existir.
        """
        p = Path(caminho) if caminho is not None else self.taxonomia_path
        if not p.exists():
            raise FileNotFoundError(f"Taxonomia não encontrada: {p}")
        with open(p, encoding="utf-8") as f:
            data = json.load(f)

        # Formato v8: {"nos": [{"id_alvo": "...", "nome": "...", "pai": ...}, ...]}
        if isinstance(data, dict) and "nos" in data:
            return [
                {
                    "id": str(c.get("id_alvo", "")),
                    "nome": c.get("nome", ""),
                    "pai_id": c.get("pai"),
                }
                for c in data["nos"]
            ]

        # Formato legado: {"categorias": [...]} ou lista direta
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

    def sincronizar(self, *, dry_run: bool = False) -> dict[str, int]:
        """Sincroniza a taxonomia v8 para o Bling.

        Args:
            dry_run: Se True, não cria categorias no Bling nem grava o mapeamento.

        Returns:
            Dict com estatísticas: {"criadas", "reutilizadas", "erros", "total"}.
        """
        if self.provider is None:
            raise ValueError("Provider não configurado. Passe um provider no construtor.")

        categorias = self.carregar_taxonomia()
        mapeamento = self.carregar_mapeamento()

        # 1. Fetch Before Sync: carrega categorias existentes no Bling
        # e indexa por (nome_normalizado, pai_id_normalizado) para idempotência
        existentes = self.provider.listar_categorias()
        bling_por_chave: dict[tuple[str, str | None], int] = {}
        for c in existentes:
            nome = c.get("nome", "").strip().lower()
            pid = c.get("pai_id")
            # Normaliza pai_id: 0, None, "" -> None (raiz)
            if pid in (0, None, ""):
                pid_norm = None
            else:
                pid_norm = str(pid)
            bling_por_chave[(nome, pid_norm)] = c["id"]

        # 2. Ordenação Hierárquica: calcula profundidade e ordena raízes primeiro
        por_id = {str(c["id"]): c for c in categorias}

        def obter_profundidade(item_id: str) -> int:
            item = por_id.get(item_id)
            if not item or not item.get("pai_id"):
                return 0
            return 1 + obter_profundidade(str(item["pai_id"]))

        categorias_ordenadas = sorted(
            categorias,
            key=lambda c: (obter_profundidade(str(c["id"])), str(c["nome"]).lower())
        )

        criadas = 0
        reutilizadas = 0
        erros = 0
        total = len(categorias_ordenadas)

        # Contador para IDs simulados no dry_run
        dry_run_counter = 0

        for idx, cat in enumerate(categorias_ordenadas, 1):
            cid_v8 = str(cat["id"])

            # Se já está no mapeamento local, conta como reutilizada e pula
            if cid_v8 in mapeamento:
                reutilizadas += 1
                # Progresso visual para itens já mapeados
                if idx % 10 == 0 or idx == total:
                    processadas = criadas + reutilizadas + erros
                    print(f"[{processadas}/{total}] Sincronizando: {cat.get('nome', cid_v8)} (já mapeada)")
                continue

            try:
                nome = cat["nome"].strip()
                pai_v8 = cat.get("pai_id")

                # 3. Mapeamento de Pai: resolve pai_id da v8 para bling_id
                bling_pai_id: int | None = None
                if pai_v8 is not None and str(pai_v8) != "":
                    pai_v8_str = str(pai_v8)
                    if pai_v8_str in mapeamento:
                        bling_pai_id = mapeamento[pai_v8_str]
                    else:
                        # Fallback: pai não mapeado (pode acontecer se a taxonomia vier desordenada
                        # e o pai ainda não foi processado; tentamos buscar no índice do Bling)
                        bling_pai_id = mapeamento.get(pai_v8_str)

                # Normaliza chave de busca no Bling
                pai_norm = str(bling_pai_id) if bling_pai_id is not None else None
                chave = (nome.lower(), pai_norm)

                # 4. Idempotência: verifica se já existe no Bling
                if chave in bling_por_chave:
                    bling_id = bling_por_chave[chave]
                    mapeamento[cid_v8] = bling_id
                    reutilizadas += 1
                else:
                    # 5. Cria no Bling (ou simula no dry_run)
                    if dry_run:
                        dry_run_counter += 1
                        bling_id = 999000 + dry_run_counter
                    else:
                        payload = {"nome": nome, "pai_id": bling_pai_id, "descricao": ""}
                        nova = self.provider.criar_categoria(payload)
                        bling_id = nova["id"]
                        # Atualiza índice local para filhos subsequentes
                        bling_por_chave[chave] = bling_id

                    mapeamento[cid_v8] = bling_id
                    criadas += 1

            except Exception as exc:
                # 6. Isolamento de Falhas: loga erro e continua
                print(f"[ERRO] Falha ao sincronizar categoria '{cat.get('nome', cid_v8)}' (id_v8={cid_v8}): {exc}")
                erros += 1
                # Não adiciona ao mapeamento - categoria falhou
                continue

            # 7. Progresso Visual: a cada 10 categorias e no final
            if idx % 10 == 0 or idx == total:
                processadas = criadas + reutilizadas + erros
                print(f"[{processadas}/{total}] Sincronizando: {cat.get('nome', cid_v8)}")

        # 8. Salva mapeamento (apenas se não dry_run)
        if not dry_run and (criadas > 0 or reutilizadas > 0):
            self.salvar_mapeamento(mapeamento)

        return {
            "criadas": criadas,
            "reutilizadas": reutilizadas,
            "erros": erros,
            "total": total,
        }
