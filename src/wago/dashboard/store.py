"""Histórico local das execuções do painel."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


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
                """
            )

    def _conectar(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.caminho, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def criar_execucao(
        self, execucao_id: str, operacao: str, parametros: str
    ) -> None:
        with self._lock, self._conectar() as conn:
            conn.execute(
                """
                INSERT INTO execucoes (id, operacao, status, inicio, parametros)
                VALUES (?, ?, 'executando', ?, ?)
                """,
                (execucao_id, operacao, _agora(), parametros),
            )

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
