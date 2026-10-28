"""Histórico e fila de execuções do painel.

A tabela `execucoes` também é a fila: o painel insere com status `na_fila`
e o worker (outro processo) reivindica, executa e fecha.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ATIVOS = ("na_fila", "executando")


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ha(segundos: float) -> str:
    return (datetime.now() - timedelta(seconds=segundos)).isoformat(timespec="seconds")


class Store:
    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conectar() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS execucoes (
                    id TEXT PRIMARY KEY,
                    operacao TEXT NOT NULL,
                    status TEXT NOT NULL,
                    inicio TEXT NOT NULL,
                    fim TEXT,
                    registros INTEGER NOT NULL DEFAULT 0,
                    mensagem TEXT NOT NULL DEFAULT '',
                    parametros TEXT NOT NULL DEFAULT '{}',
                    codigo INTEGER,
                    arquivos TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS etapas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execucao_id TEXT NOT NULL,
                    etapa TEXT NOT NULL,
                    status TEXT NOT NULL,
                    inicio TEXT NOT NULL,
                    fim TEXT,
                    registros INTEGER NOT NULL DEFAULT 0,
                    mensagem TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS eventos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execucao_id TEXT NOT NULL,
                    etapa TEXT NOT NULL,
                    nivel TEXT NOT NULL,
                    quando TEXT NOT NULL,
                    mensagem TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config (
                    chave TEXT PRIMARY KEY,
                    valor TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    pid INTEGER NOT NULL,
                    batimento TEXT NOT NULL
                );
                """
            )
            colunas = {row["name"] for row in conn.execute("PRAGMA table_info(execucoes)")}
            for nome, tipo in (
                ("cancelar", "INTEGER NOT NULL DEFAULT 0"),
                ("batimento", "TEXT"),
                ("worker_pid", "INTEGER"),
            ):
                if nome not in colunas:
                    conn.execute(f"ALTER TABLE execucoes ADD COLUMN {nome} {tipo}")
            conn.execute("PRAGMA journal_mode=WAL")

    def _conectar(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.caminho, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _transacao(self):
        with self._lock:
            conn = sqlite3.connect(self.caminho, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
            finally:
                conn.close()

    # ---- fila -----------------------------------------------------------

    def enfileirar(self, execucao_id: str, operacao: str, parametros: str) -> bool:
        """Insere na fila. Recusa (False) se já houver execução na fila ou rodando."""
        with self._transacao() as conn:
            ativa = conn.execute(
                "SELECT 1 FROM execucoes WHERE status IN (?, ?) LIMIT 1", ATIVOS
            ).fetchone()
            if ativa:
                return False
            conn.execute(
                """
                INSERT INTO execucoes (id, operacao, status, inicio, parametros)
                VALUES (?, ?, 'na_fila', ?, ?)
                """,
                (execucao_id, operacao, _agora(), parametros),
            )
            return True

    def criar_execucao(
        self, execucao_id: str, operacao: str, parametros: str
    ) -> None:
        if not self.enfileirar(execucao_id, operacao, parametros):
            raise RuntimeError("Já existe uma execução em andamento.")

    def reivindicar(self, pid: int) -> dict[str, Any] | None:
        """Pega a execução mais antiga da fila. Atômico entre processos."""
        with self._transacao() as conn:
            row = conn.execute(
                "SELECT * FROM execucoes WHERE status='na_fila' ORDER BY inicio, rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE execucoes SET status='executando', worker_pid=?, batimento=?
                WHERE id=?
                """,
                (pid, _agora(), row["id"]),
            )
        dados = dict(row)
        dados["status"] = "executando"
        return dados

    def batimento(self, execucao_id: str) -> bool:
        """Renova o batimento da execução. Devolve True se o cancelamento foi pedido."""
        with self._transacao() as conn:
            conn.execute(
                "UPDATE execucoes SET batimento=? WHERE id=?", (_agora(), execucao_id)
            )
            row = conn.execute(
                "SELECT cancelar FROM execucoes WHERE id=?", (execucao_id,)
            ).fetchone()
        return bool(row and row["cancelar"])

    def pedir_cancelamento(self, execucao_id: str) -> str | None:
        """Devolve o novo status, ou None se a execução não estiver ativa."""
        with self._transacao() as conn:
            row = conn.execute(
                "SELECT status FROM execucoes WHERE id=?", (execucao_id,)
            ).fetchone()
            if row is None or row["status"] not in ATIVOS:
                return None
            if row["status"] == "na_fila":
                conn.execute(
                    """
                    UPDATE execucoes SET status='cancelada', fim=?, codigo=2,
                        mensagem='Cancelada antes de sair da fila.'
                    WHERE id=?
                    """,
                    (_agora(), execucao_id),
                )
                return "cancelada"
            conn.execute("UPDATE execucoes SET cancelar=1 WHERE id=?", (execucao_id,))
            return "cancelando"

    def execucao_ativa(self) -> dict[str, Any] | None:
        with self._lock, self._conectar() as conn:
            row = conn.execute(
                "SELECT * FROM execucoes WHERE status IN (?, ?) ORDER BY inicio LIMIT 1",
                ATIVOS,
            ).fetchone()
            if row is None:
                return None
            etapa = conn.execute(
                """
                SELECT etapa FROM etapas WHERE execucao_id=? AND status='executando'
                ORDER BY id DESC LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
        dados = dict(row)
        dados["etapa"] = etapa["etapa"] if etapa else ""
        return dados

    def marcar_orfas(self, limite_s: float = 60) -> list[str]:
        """Execução 'executando' sem batimento recente: o worker morreu."""
        limite = _ha(limite_s)
        with self._transacao() as conn:
            rows = conn.execute(
                """
                SELECT id FROM execucoes
                WHERE status='executando' AND COALESCE(batimento, inicio) < ?
                """,
                (limite,),
            ).fetchall()
            ids = [row["id"] for row in rows]
            for execucao_id in ids:
                conn.execute(
                    """
                    UPDATE execucoes SET status='interrompida', fim=?, codigo=3,
                        mensagem='Worker parou sem fechar a execução. Rode de novo para retomar do catálogo.'
                    WHERE id=?
                    """,
                    (_agora(), execucao_id),
                )
                conn.execute(
                    """
                    UPDATE etapas SET status='interrompida', fim=?
                    WHERE execucao_id=? AND status='executando'
                    """,
                    (_agora(), execucao_id),
                )
        return ids

    # ---- worker ---------------------------------------------------------

    def batimento_worker(self, pid: int) -> None:
        with self._transacao() as conn:
            conn.execute(
                """
                INSERT INTO worker (id, pid, batimento) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET pid=excluded.pid, batimento=excluded.batimento
                """,
                (pid, _agora()),
            )

    def encerrar_worker(self, pid: int) -> None:
        with self._transacao() as conn:
            conn.execute("DELETE FROM worker WHERE pid=?", (pid,))

    def worker_vivo(self, limite_s: float = 20) -> dict[str, Any] | None:
        with self._lock, self._conectar() as conn:
            row = conn.execute(
                "SELECT pid, batimento FROM worker WHERE id=1 AND batimento >= ?",
                (_ha(limite_s),),
            ).fetchone()
        return dict(row) if row else None

    def fechar_execucao(
        self,
        execucao_id: str,
        *,
        status: str,
        registros: int,
        mensagem: str,
        codigo: int,
        arquivos: str,
    ) -> None:
        with self._lock, self._conectar() as conn:
            conn.execute(
                """
                UPDATE execucoes
                SET status=?, fim=?, registros=?, mensagem=?, codigo=?, arquivos=?
                WHERE id=?
                """,
                (status, _agora(), registros, mensagem, codigo, arquivos, execucao_id),
            )

    def iniciar_etapa(self, execucao_id: str, etapa: str) -> int:
        with self._lock, self._conectar() as conn:
            cur = conn.execute(
                """
                INSERT INTO etapas (execucao_id, etapa, status, inicio)
                VALUES (?, ?, 'executando', ?)
                """,
                (execucao_id, etapa, _agora()),
            )
            return int(cur.lastrowid)

    def fechar_etapa(
        self,
        etapa_id: int,
        *,
        status: str,
        registros: int,
        mensagem: str,
    ) -> None:
        with self._lock, self._conectar() as conn:
            conn.execute(
                """
                UPDATE etapas
                SET status=?, fim=?, registros=?, mensagem=?
                WHERE id=?
                """,
                (status, _agora(), registros, mensagem, etapa_id),
            )

    def evento(
        self, execucao_id: str, etapa: str, nivel: str, mensagem: str
    ) -> None:
        with self._lock, self._conectar() as conn:
            conn.execute(
                """
                INSERT INTO eventos (execucao_id, etapa, nivel, quando, mensagem)
                VALUES (?, ?, ?, ?, ?)
                """,
                (execucao_id, etapa, nivel, _agora(), mensagem[:2000]),
            )

    def listar_execucoes(self, limite: int = 40) -> list[dict[str, Any]]:
        with self._lock, self._conectar() as conn:
            rows = conn.execute(
                """
                SELECT * FROM execucoes ORDER BY inicio DESC LIMIT ?
                """,
                (limite,),
            ).fetchall()
        return [dict(row) for row in rows]

    def obter_execucao(self, execucao_id: str) -> dict[str, Any] | None:
        with self._lock, self._conectar() as conn:
            row = conn.execute(
                "SELECT * FROM execucoes WHERE id=?", (execucao_id,)
            ).fetchone()
            if row is None:
                return None
            etapas = conn.execute(
                """
                SELECT * FROM etapas WHERE execucao_id=? ORDER BY id
                """,
                (execucao_id,),
            ).fetchall()
        dados = dict(row)
        dados["etapas"] = [dict(item) for item in etapas]
        return dados

    def listar_eventos(
        self,
        *,
        execucao_id: str | None = None,
        etapa: str | None = None,
        nivel: str | None = None,
        limite: int = 200,
    ) -> list[dict[str, Any]]:
        clausulas = []
        valores: list[Any] = []
        if execucao_id:
            clausulas.append("execucao_id=?")
            valores.append(execucao_id)
        if etapa:
            clausulas.append("etapa=?")
            valores.append(etapa)
        if nivel:
            clausulas.append("nivel=?")
            valores.append(nivel)
        where = f"WHERE {' AND '.join(clausulas)}" if clausulas else ""
        valores.append(limite)
        with self._lock, self._conectar() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM eventos {where}
                ORDER BY id DESC LIMIT ?
                """,
                valores,
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def ultimas_etapas(self) -> dict[str, dict[str, Any]]:
        with self._lock, self._conectar() as conn:
            rows = conn.execute(
                """
                SELECT e.* FROM etapas e
                JOIN (
                    SELECT etapa, MAX(id) AS id FROM etapas GROUP BY etapa
                ) u ON u.id = e.id
                """
            ).fetchall()
        return {row["etapa"]: dict(row) for row in rows}

    def contar_status(self) -> dict[str, int]:
        with self._lock, self._conectar() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM execucoes GROUP BY status"
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def ler_config(self) -> dict[str, str]:
        with self._lock, self._conectar() as conn:
            rows = conn.execute("SELECT chave, valor FROM config").fetchall()
        return {row["chave"]: row["valor"] for row in rows}

    def gravar_config(self, valores: dict[str, str]) -> None:
        with self._lock, self._conectar() as conn:
            for chave, valor in valores.items():
                conn.execute(
                    """
                    INSERT INTO config (chave, valor) VALUES (?, ?)
                    ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor
                    """,
                    (chave, valor),
                )

    def resetar(self) -> None:
        """Apaga histórico de execuções, etapas e eventos para reiniciar os contadores."""
        with self._lock, self._conectar() as conn:
            conn.executescript(
                """
                DELETE FROM eventos;
                DELETE FROM etapas;
                DELETE FROM execucoes;
                """
            )
