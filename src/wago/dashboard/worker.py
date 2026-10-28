"""Worker da fila do painel.

Roda em processo próprio (`python -m wago worker`): o painel só enfileira em
`dashboard.sqlite`. Fechar o painel não interrompe o que está em execução.
Cancelamento chega pela coluna `cancelar` e é lido a cada batimento.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from wago.dashboard.service import (
    EXPORTERS,
    ORDEM_PIPELINE,
    Cancelado,
    ErroPainel,
    _Coletor,
    _nomes_saida,
    master_util,
    redigir,
    texto_falha,
)
from wago.dashboard.store import Store

INTERVALO_BATIMENTO = 0.5
APOS_ETAPA = {"fill-nuvemshop", "images"}


class Worker:
    def __init__(self, store: Store, acoes: dict[str, Callable]):
        self.store = store
        self.acoes = acoes
        self.pid = os.getpid()
        self._cancel = threading.Event()
        self._etapa_atual = ""

    def rodar(self, ocioso_max: float = 120.0, intervalo: float = 1.0) -> None:
        """Consome a fila até ficar `ocioso_max` segundos sem trabalho."""
        self.store.marcar_orfas()
        self.store.batimento_worker(self.pid)
        ocioso_desde = time.monotonic()
        try:
            while True:
                if self.processar_proxima():
                    ocioso_desde = time.monotonic()
                    continue
                if time.monotonic() - ocioso_desde >= ocioso_max:
                    break
                time.sleep(intervalo)
                self.store.batimento_worker(self.pid)
        finally:
            self.store.encerrar_worker(self.pid)

    def processar_proxima(self) -> bool:
        execucao = self.store.reivindicar(self.pid)
        if execucao is None:
            return False
        params = json.loads(execucao.get("parametros") or "{}")
        self._cancel.clear()
        parar = threading.Event()
        monitor = threading.Thread(
            target=self._monitorar, args=(execucao["id"], parar), daemon=True
        )
        monitor.start()
        try:
            self._trabalhar(execucao["id"], execucao["operacao"], params)
        finally:
            parar.set()
            monitor.join(5)
        return True

    def _monitorar(self, execucao_id: str, parar: threading.Event) -> None:
        while not parar.wait(INTERVALO_BATIMENTO):
            try:
                if self.store.batimento(execucao_id):
                    self._cancel.set()
                self.store.batimento_worker(self.pid)
            except Exception:
                continue

    def _trabalhar(self, execucao_id: str, operacao: str, params: dict[str, Any]) -> None:
        antes = _nomes_saida()
        registros = 0
        mensagem = ""
        status = "concluida"
        codigo = 0
        try:
            etapas = ORDEM_PIPELINE if operacao == "pipeline" else (operacao,)
            for etapa in etapas:
                if self._cancel.is_set():
                    status = "cancelada"
                    codigo = 2
                    mensagem = "Cancelada antes da próxima etapa."
                    break
                if etapa in EXPORTERS and not master_util(params.get("master") or ""):
                    status = "falha"
                    codigo = 1
                    mensagem = "Exportadores não rodaram: ainda não há master válido."
                    self.store.evento(execucao_id, etapa, "erro", mensagem)
                    break
                resultado, etapa_status, texto = self._rodar_etapa(
                    execucao_id, etapa, params
                )
                registros += resultado
                mensagem = texto or mensagem
                if etapa_status == "cancelada":
                    status = "cancelada"
                    codigo = 2
                    break
                if etapa_status == "falha":
                    status = "falha"
                    codigo = 1
                    break
                if etapa_status == "concluida_com_avisos" and status == "concluida":
                    status = "concluida_com_avisos"
        except Exception as exc:
            status = "falha"
            codigo = 1
            mensagem = redigir(f"{type(exc).__name__}: {exc}")
            self.store.evento(execucao_id, self._etapa_atual or operacao, "erro", mensagem)
        finally:
            novos = sorted(_nomes_saida() - antes)
            self.store.fechar_execucao(
                execucao_id,
                status=status,
                registros=registros,
                mensagem=mensagem,
                codigo=codigo,
                arquivos=json.dumps(novos, ensure_ascii=False),
            )
            self._etapa_atual = ""

    def _rodar_etapa(
        self, execucao_id: str, etapa: str, params: dict[str, Any]
    ) -> tuple[int, str, str]:
        self._etapa_atual = etapa
        etapa_id = self.store.iniciar_etapa(execucao_id, etapa)
        coletor = _Coletor(
            self.store, execucao_id, lambda: self._etapa_atual, sys.stdout
        )
        status = "concluida"
        registros = 0
        mensagem = ""
        anterior = sys.stdout
        anterior_err = sys.stderr
        try:
            sys.stdout = coletor
            sys.stderr = coletor
            if self._cancel.is_set():
                raise Cancelado()
            acao = self.acoes[etapa]
            registros = int(acao(params, self._cancel.is_set) or 0)
            if etapa in APOS_ETAPA and not params.get("dry_run"):
                _sincronizar_catalogo()
            if self._cancel.is_set():
                raise Cancelado()
            if coletor.erros or coletor.avisos:
                status = "concluida_com_avisos"
        except Cancelado:
            status = "cancelada"
            mensagem = "Cancelada. Arquivos já gravados foram preservados."
        except (ErroPainel, SystemExit) as exc:
            status = "falha"
            mensagem = texto_falha(exc)
            self.store.evento(execucao_id, etapa, "erro", mensagem)
            anterior.write(mensagem + "\n")
            anterior.flush()
        except Exception as exc:
            status = "falha"
            mensagem = redigir(f"Falha: {type(exc).__name__}: {exc}")
            self.store.evento(execucao_id, etapa, "erro", mensagem)
            anterior.write(mensagem + "\n")
            anterior.flush()
        finally:
            sys.stdout = anterior
            sys.stderr = anterior_err
            coletor.flush()
        if not mensagem:
            eventos = self.store.listar_eventos(execucao_id=execucao_id, etapa=etapa, limite=1)
            mensagem = eventos[-1]["mensagem"] if eventos else ""
        self.store.fechar_etapa(
            etapa_id, status=status, registros=registros, mensagem=mensagem
        )
        return registros, status, mensagem


def _sincronizar_catalogo() -> None:
    from wago.state import sincronizar_saidas

    sincronizar_saidas()


def main(ocioso_max: float = 120.0) -> None:
    from wago.config import STATE, ensure_dirs, load_env
    from wago.dashboard.service import ACOES_REAIS

    load_env()
    ensure_dirs()
    store = Store(STATE / "dashboard.sqlite")
    vivo = store.worker_vivo()
    if vivo and vivo["pid"] != os.getpid():
        print(f"Worker já ativo (pid {vivo['pid']}). Saindo.", flush=True)
        return
    print(f"Worker {os.getpid()} consumindo a fila de {STATE / 'dashboard.sqlite'}", flush=True)
    Worker(store, ACOES_REAIS).rodar(ocioso_max=ocioso_max)
    print("Fila vazia. Worker encerrado.", flush=True)
