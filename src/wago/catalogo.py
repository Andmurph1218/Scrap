"""Máquina de estados por SKU, persistida em SQLite.

Fluxo:
    DESCOBERTO -> AGUARDANDO_SCRAPE -> SCRAPE_OK -> AGUARDANDO_IMAGEM -> EXPORTADO
                                    \\-> FILTRADO_SEM_CUSTO (terminal)

Uma falha no scrape mantém o SKU em AGUARDANDO_SCRAPE, com `tentativas` e
`ultimo_erro` atualizados. Transição fora do grafo é ignorada, o que torna
toda gravação idempotente: repetir uma etapa não regride nem duplica SKU.
"""

from __future__ import annotations

import random
import re
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DESCOBERTO = "DESCOBERTO"
AGUARDANDO_SCRAPE = "AGUARDANDO_SCRAPE"
FILTRADO_SEM_CUSTO = "FILTRADO_SEM_CUSTO"
SCRAPE_OK = "SCRAPE_OK"
AGUARDANDO_IMAGEM = "AGUARDANDO_IMAGEM"
EXPORTADO = "EXPORTADO"

STATUS = (
    DESCOBERTO,
    AGUARDANDO_SCRAPE,
    FILTRADO_SEM_CUSTO,
    SCRAPE_OK,
    AGUARDANDO_IMAGEM,
    EXPORTADO,
)
POS_SCRAPE = (SCRAPE_OK, AGUARDANDO_IMAGEM, EXPORTADO)


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def chave_pasta(sku: str) -> str:
    """Mesma normalização de `images.pasta_produto` para casar SKU e pasta."""
    return (re.sub(r"[^\w-]+", "_", str(sku).strip()).strip("._") or "").upper()


class Catalogo:
    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        with self._conexao() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS produtos (
                    sku TEXT PRIMARY KEY,
                    url TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    ordem INTEGER NOT NULL,
                    tentativas INTEGER NOT NULL DEFAULT 0,
                    ultimo_erro TEXT NOT NULL DEFAULT '',
                    lote INTEGER,
                    criado_em TEXT NOT NULL,
                    atualizado_em TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_produtos_fila
                    ON produtos(status, tentativas, ordem);
                CREATE TABLE IF NOT EXISTS meta (
                    chave TEXT PRIMARY KEY,
                    valor TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _conexao(self):
        conn = sqlite3.connect(self.caminho, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transacao(self):
        with self._conexao() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # ---- meta -----------------------------------------------------------

    def meta(self, chave: str) -> str | None:
        with self._conexao() as conn:
            row = conn.execute("SELECT valor FROM meta WHERE chave=?", (chave,)).fetchone()
        return row["valor"] if row else None

    def gravar_meta(self, chave: str, valor: str) -> None:
        with self._transacao() as conn:
            conn.execute(
                "INSERT INTO meta (chave, valor) VALUES (?, ?) "
                "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
                (chave, valor),
            )

    # ---- transições -----------------------------------------------------

    def registrar_descobertos(self, links: Iterable[str]) -> int:
        """Insere SKUs novos como DESCOBERTO. A posição na fila é sorteada uma vez."""
        from wago.discover import sku_from_url

        agora = _agora()
        linhas = []
        for url in links:
            sku = sku_from_url(str(url))
            if sku:
                linhas.append((sku, str(url), DESCOBERTO, random.getrandbits(48), agora, agora))
        with self._transacao() as conn:
            antes = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO produtos
                    (sku, url, status, ordem, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                linhas,
            )
            return conn.total_changes - antes

    def enfileirar(self) -> int:
        """DESCOBERTO -> AGUARDANDO_SCRAPE."""
        return self._mover(DESCOBERTO, AGUARDANDO_SCRAPE)

    def fila_scrape(self) -> list[tuple[str, str]]:
        """SKUs pendentes na ordem persistida; os que já falharam vão para o fim."""
        with self._conexao() as conn:
            rows = conn.execute(
                """
                SELECT sku, url FROM produtos WHERE status=?
                ORDER BY tentativas, ordem
                """,
                (AGUARDANDO_SCRAPE,),
            ).fetchall()
        return [(row["sku"], row["url"]) for row in rows]

    def marcar_scrape_ok(self, sku: str, lote: int | None = None) -> None:
        self._transicionar(
            sku, SCRAPE_OK, (DESCOBERTO, AGUARDANDO_SCRAPE), lote=lote, ultimo_erro=""
        )

    def marcar_sem_custo(self, sku: str) -> None:
        self._transicionar(sku, FILTRADO_SEM_CUSTO, (DESCOBERTO, AGUARDANDO_SCRAPE))

    def registrar_falha(self, sku: str, motivo: str) -> None:
        """Mantém o SKU na fila e contabiliza a tentativa."""
        sku = sku.strip().upper()
        with self._transacao() as conn:
            conn.execute(
                """
                UPDATE produtos
                SET tentativas=tentativas+1, ultimo_erro=?, atualizado_em=?,
                    status=?
                WHERE sku=? AND status IN (?, ?)
                """,
                (motivo[:500], _agora(), AGUARDANDO_SCRAPE, sku, DESCOBERTO, AGUARDANDO_SCRAPE),
            )

    def reconciliar_saidas(self, skus_exportados: set[str], pastas_com_imagem: set[str]) -> None:
        """
        Deriva o estado pós-scrape das saídas em disco:
        no CSV Nuvemshop e com fotos -> EXPORTADO; só no CSV -> AGUARDANDO_IMAGEM;
        fora do CSV -> SCRAPE_OK.
        """
        exportados = {s.strip().upper() for s in skus_exportados if s.strip()}
        pastas = {p.upper() for p in pastas_com_imagem}
        agora = _agora()
        with self._transacao() as conn:
            rows = conn.execute(
                f"SELECT sku, status FROM produtos WHERE status IN ({','.join('?' * len(POS_SCRAPE))})",
                POS_SCRAPE,
            ).fetchall()
            mudancas = []
            for row in rows:
                sku = row["sku"]
                if sku in exportados:
                    novo = EXPORTADO if chave_pasta(sku) in pastas else AGUARDANDO_IMAGEM
                else:
                    novo = SCRAPE_OK
                if novo != row["status"]:
                    mudancas.append((novo, agora, sku))
            conn.executemany(
                "UPDATE produtos SET status=?, atualizado_em=? WHERE sku=?", mudancas
            )

    def _mover(self, origem: str, destino: str) -> int:
        with self._transacao() as conn:
            cur = conn.execute(
                "UPDATE produtos SET status=?, atualizado_em=? WHERE status=?",
                (destino, _agora(), origem),
            )
            return cur.rowcount

    def _transicionar(
        self, sku: str, destino: str, origens: tuple[str, ...], **campos
    ) -> None:
        sku = sku.strip().upper()
        if not sku:
            return
        agora = _agora()
        extras = {k: v for k, v in campos.items() if v is not None}
        sets = ", ".join(f"{k}=?" for k in extras)
        with self._transacao() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO produtos
                    (sku, status, ordem, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sku, DESCOBERTO, random.getrandbits(48), agora, agora),
            )
            conn.execute(
                f"""
                UPDATE produtos SET status=?, atualizado_em=?{', ' + sets if sets else ''}
                WHERE sku=? AND status IN ({','.join('?' * len(origens))})
                """,
                (destino, agora, *extras.values(), sku, *origens),
            )

    # ---- leitura --------------------------------------------------------

    def status_de(self, sku: str) -> str | None:
        with self._conexao() as conn:
            row = conn.execute(
                "SELECT status FROM produtos WHERE sku=?", (sku.strip().upper(),)
            ).fetchone()
        return row["status"] if row else None

    def skus(self, *status: str) -> set[str]:
        with self._conexao() as conn:
            rows = conn.execute(
                f"SELECT sku FROM produtos WHERE status IN ({','.join('?' * len(status))})",
                status,
            ).fetchall()
        return {row["sku"] for row in rows}

    def contagem(self) -> dict[str, int]:
        with self._conexao() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM produtos GROUP BY status"
            ).fetchall()
            com_falha = conn.execute(
                "SELECT COUNT(*) FROM produtos WHERE status=? AND tentativas>0",
                (AGUARDANDO_SCRAPE,),
            ).fetchone()[0]
        dados = {status: 0 for status in STATUS}
        dados.update({row["status"]: row["n"] for row in rows})
        dados["total"] = sum(dados[s] for s in STATUS)
        dados["com_falha"] = int(com_falha)
        return dados

    def resetar(self) -> None:
        with self._transacao() as conn:
            conn.execute("DELETE FROM produtos")
