"""Valida pedidos do painel, enfileira e expõe o estado do pipeline."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from wago.catalogo import (
    AGUARDANDO_IMAGEM,
    EXPORTADO,
    FILTRADO_SEM_CUSTO,
    POS_SCRAPE,
)
from wago.config import (
    LINKS_FILE,
    OUT,
    ROOT,
    SKUS_PROCESSADOS,
    SKUS_SEM_PRECO,
    STATE,
    ensure_dirs,
)
from wago.dashboard.store import Store
from wago.state import catalogo

OPERACOES = (
    "discover",
    "scrape",
    "export",
    "fill-nuvemshop",
    "images",
    "fill-ml",
    "photos",
    "pipeline",
    "tax-inventario",
    "tax-inventario-bling",
    "tax-classificar",
    "tax-classificar-bling",
    "tax-atribuir",
    "tax-atribuir-bling",
    "tax-desfazer",
    "tax-arquivar",
    "tax-proposta",
    "tax-aprovar-taxonomia",
    "tax-plano",
    "tax-aplicar",
    "tax-plano-bling",
    "tax-aplicar-bling",
    "gavazzi-scrap",
    "gavazzi-vincular-fotos",
)
ORDEM_PIPELINE = (
    "discover",
    "scrape",
    "export",
    "fill-nuvemshop",
    "images",
    "fill-ml",
)
ETAPAS_PAINEL = (
    "discover",
    "scrape",
    "export",
    "fill-nuvemshop",
    "images",
    "fill-ml",
    "photos",
)
EXPORTERS = {"export", "fill-nuvemshop", "images", "fill-ml", "photos"}
NOMES = {
    "discover": "Discover links",
    "scrape": "Scrape autenticado",
    "export": "Export master",
    "fill-nuvemshop": "Preencher Nuvemshop",
    "images": "Baixar imagens",
    "fill-ml": "Preencher Mercado Livre",
    "photos": "Vincular fotos Nuvemshop",
    "tax-inventario": "Taxonomia: inventário da loja",
    "tax-inventario-bling": "Taxonomia: reler inventário (Bling)",
    "tax-classificar": "Taxonomia: classificar produtos",
    "tax-classificar-bling": "Taxonomia: classificar produtos (Bling)",
    "tax-atribuir": "Taxonomia: atribuir categoria em massa",
    "tax-desfazer": "Taxonomia: desfazer decisões",
    "tax-arquivar": "Taxonomia: marcar para arquivar",
    "tax-proposta": "Taxonomia: decidir proposta da IA",
    "tax-aprovar-taxonomia": "Taxonomia: aprovar nova versão",
    "tax-plano": "Taxonomia: gerar plano de mudanças",
    "tax-aplicar": "Taxonomia: aplicar plano na loja",
    "tax-plano-bling": "Taxonomia: gerar plano de mudanças (Bling)",
    "tax-aplicar-bling": "Taxonomia: aplicar plano na loja (Bling)",
    "gavazzi-scrap": "Gavazzi: scrap + tradução + planilhas",
    "gavazzi-vincular-fotos": "Gavazzi: vincular fotos Nuvemshop",
}
MAX_IDS_LOTE = 20000
ABAS_FORA = {"Ajuda", "extra info", "Textos jurídicos"}
CONFIG_PADRAO = {
    "limite": "3",
    "timeout": "30",
    "tentativas": "3",
    "aba": "Automático",
}
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_SEGREDO = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?\S+|bearer\s+\S+"
    r"|cookie\s*[:=]\s*\S+|token\s*[:=]\s*\S+"
    r"|password\s*[:=]\s*\S+|senha\s*[:=]\s*\S+)"
)
_CHAVES_SECRETAS = (
    "WAGO_PASS",
    "NUVEMSHOP_ACCESS_TOKEN",
    "NUVEMSHOP_CLIENT_SECRET",
    "ML_CLIENT_SECRET",
    "ML_REFRESH_TOKEN",
    "ML_ACCESS_TOKEN",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEVIN_API_KEY",
)


class ErroPainel(Exception):
    def __init__(self, status: int, mensagem: str):
        super().__init__(mensagem)
        self.status = status
        self.mensagem = mensagem


class Pausado(Exception):
    def __init__(self, mensagem: str, registros: int = 0):
        super().__init__(mensagem)
        self.registros = registros


class Cancelado(Exception):
    pass


def redigir(texto: str) -> str:
    limpo = _SEGREDO.sub("[redigido]", texto)
    for chave in _CHAVES_SECRETAS:
        valor = os.getenv(chave) or ""
        if len(valor) >= 4:
            limpo = limpo.replace(valor, "[redigido]")
    return limpo


def texto_falha(exc: BaseException) -> str:
    """Mensagem de SystemExit. Código numérico sozinho não explica a falha."""
    if isinstance(exc, SystemExit):
        codigo = exc.code
        if isinstance(codigo, int):
            return f"Falha: a etapa encerrou com código {codigo}, sem mensagem."
        if codigo:
            return redigir(str(codigo))
        return "Falha: a etapa encerrou sem mensagem."
    return redigir(str(exc) or "Falha: a etapa não concluiu.")


def nivel_da_linha(linha: str) -> str:
    if re.search(r"(?i)\berro\b|falha\b|traceback|exception", linha):
        return "erro"
    if re.search(r"(?i)\baviso\b|pendente|rejeitad", linha):
        return "aviso"
    return "info"


def abas_do_template() -> list[str]:
    caminho = ROOT / "templates" / "Anunciar-ML.xlsx"
    if not caminho.exists():
        return ["Automático"]
    with zipfile.ZipFile(caminho) as arquivo:
        livro = ET.fromstring(arquivo.read("xl/workbook.xml"))
    nomes = ["Automático"]
    for sheet in livro.findall("m:sheets/m:sheet", _NS):
        nome = sheet.attrib.get("name") or ""
        if nome and nome not in ABAS_FORA:
            nomes.append(nome)
    return nomes


def _inteiro(valor: Any, chave: str, minimo: int, maximo: int) -> int:
    if isinstance(valor, bool):
        raise ErroPainel(400, f"{chave} inválido")
    try:
        numero = int(valor)
    except (TypeError, ValueError) as exc:
        raise ErroPainel(400, f"{chave} inválido") from exc
    if not minimo <= numero <= maximo:
        raise ErroPainel(400, f"{chave} deve ficar entre {minimo} e {maximo}")
    return numero


def _bool(valor: Any, chave: str, padrao: bool = False) -> bool:
    if valor is None:
        return padrao
    if not isinstance(valor, bool):
        raise ErroPainel(400, f"{chave} inválido")
    return valor


class _Coletor:
    def __init__(self, store: Store, execucao_id: str, etapa, original):
        self.store = store
        self.execucao_id = execucao_id
        self.etapa = etapa
        self.original = original
        self._buf = ""
        self.avisos = 0
        self.erros = 0

    def write(self, texto: str) -> int:
        self._buf += texto
        while "\n" in self._buf:
            linha, self._buf = self._buf.split("\n", 1)
            self._guardar(linha)
        try:
            self.original.write(texto)
        except UnicodeEncodeError:
            enc = getattr(self.original, "encoding", None) or "utf-8"
            seguro = texto.encode(enc, errors="replace").decode(enc, errors="replace")
            self.original.write(seguro)
        return len(texto)

    def flush(self) -> None:
        if self._buf.strip():
            self._guardar(self._buf)
            self._buf = ""
        self.original.flush()

    def _guardar(self, linha: str) -> None:
        texto = redigir(linha.strip())
        if not texto:
            return
        nivel = nivel_da_linha(texto)
        if nivel == "aviso":
            self.avisos += 1
        elif nivel == "erro":
            self.erros += 1
        self.store.evento(self.execucao_id, self.etapa(), nivel, texto)


class Painel:
    """
    Só valida e enfileira. Quem executa é o worker, em outro processo.

    Com `acoes` injetadas (testes), o worker roda numa thread deste processo
    e drena a fila; sem elas, um `python -m wago worker` destacado é iniciado
    quando não houver worker vivo.
    """

    def __init__(self, store: Store, acoes: dict[str, Callable] | None = None):
        self.store = store
        self.acoes = acoes
        self._thread: threading.Thread | None = None
        self._spawn_lock = threading.Lock()
        self._ultimo_spawn = 0.0

    def configuracao(self) -> dict[str, Any]:
        gravada = self.store.ler_config()
        valores = dict(CONFIG_PADRAO)
        valores.update(gravada)
        return {
            "limite": int(valores["limite"]),
            "timeout": int(valores["timeout"]),
            "tentativas": int(valores["tentativas"]),
            "aba": valores["aba"],
            "diretorios": {
                "entrada": str(ROOT / "data" / "raw"),
                "saida": str(OUT),
                "estado": str(ROOT / "data" / "state"),
            },
            "templates": {
                "nuvemshop": str(ROOT / "templates" / "produtos-Nuvemshop.csv"),
                "mercadolivre": str(ROOT / "templates" / "Anunciar-ML.xlsx"),
            },
        }

    def gravar_configuracao(self, dados: dict[str, Any]) -> dict[str, Any]:
        abas = abas_do_template()
        aba = str(dados.get("aba") or CONFIG_PADRAO["aba"])
        if abas and aba not in abas:
            raise ErroPainel(400, "Aba do Mercado Livre não existe no template.")
        limite = _inteiro(dados.get("limite", CONFIG_PADRAO["limite"]), "limite", 1, 5000)
        timeout = _inteiro(dados.get("timeout", CONFIG_PADRAO["timeout"]), "timeout", 5, 180)
        tentativas = _inteiro(
            dados.get("tentativas", CONFIG_PADRAO["tentativas"]), "tentativas", 1, 5
        )
        self.store.gravar_config(
            {
                "limite": str(limite),
                "timeout": str(timeout),
                "tentativas": str(tentativas),
                "aba": aba,
            }
        )
        return self.configuracao()

    def opcoes(self) -> dict[str, Any]:
        return {
            "operacoes": [
                {"id": chave, "nome": NOMES[chave]}
                for chave in ETAPAS_PAINEL
            ],
            "abas": abas_do_template(),
            "masters": listar_masters(),
            "config": self.configuracao(),
        }

    def iniciar(self, dados: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(dados, dict):
            raise ErroPainel(400, "Pedido inválido.")
        if dados.get("confirmar") is not True:
            raise ErroPainel(400, "Confirme a execução antes de iniciar.")
        operacao = dados.get("operacao")
        if operacao not in OPERACOES:
            raise ErroPainel(400, "Operação não autorizada.")
        if operacao.startswith("tax-"):
            params = self._parametros_tax(operacao, dados)
        elif operacao == "gavazzi-vincular-fotos":
            params = self._parametros_gavazzi_fotos(dados)
        elif operacao.startswith("gavazzi-"):
            params = self._parametros_gavazzi(dados)
        else:
            params = self._parametros(dados)
        if operacao in EXPORTERS and not master_util(params.get("master") or ""):
            raise ErroPainel(
                400,
                "Nenhum master válido em data/out. Rode o scrape antes dos exportadores.",
            )
        self.store.marcar_orfas()
        execucao_id = uuid.uuid4().hex[:12]
        if not self.store.enfileirar(
            execucao_id, operacao, json.dumps(params, ensure_ascii=False)
        ):
            raise ErroPainel(409, "Já existe uma execução em andamento.")
        self.garantir_worker()
        return {"id": execucao_id, "status": "na_fila"}

    def garantir_worker(self) -> None:
        with self._spawn_lock:
            if self.acoes is not None:
                if self._thread is None or not self._thread.is_alive():
                    from wago.dashboard.worker import Worker

                    worker = Worker(self.store, self.acoes)
                    self._thread = threading.Thread(
                        target=worker.rodar,
                        kwargs={"ocioso_max": 0, "intervalo": 0.05},
                        name="wago-worker",
                        daemon=True,
                    )
                    self._thread.start()
                return
            if self.store.worker_vivo():
                return
            if time.monotonic() - self._ultimo_spawn < 10:
                return
            self._ultimo_spawn = time.monotonic()
            iniciar_worker_destacado()

    def cancelar(self, execucao_id: str) -> dict[str, str]:
        ativa = self.store.execucao_ativa()
        novo = self.store.pedir_cancelamento(execucao_id)
        if novo is None:
            raise ErroPainel(404, "Essa execução não está em andamento.")
        self.store.evento(
            execucao_id,
            (ativa or {}).get("etapa") or "painel",
            "aviso",
            "Cancelamento pedido. A gravação atual termina antes de parar."
            if novo == "cancelando"
            else "Cancelamento pedido. A execução saiu da fila antes de começar.",
        )
        return {"id": execucao_id, "status": novo}

    def painel(self) -> dict[str, Any]:
        self.store.marcar_orfas()
        execucoes = self.store.listar_execucoes(1)
        ultima = execucoes[0] if execucoes else None
        contagem = self.store.contar_status()
        ind = indicadores(contagem)
        fluxo = funil()
        ativa = self.store.execucao_ativa()
        worker = self.store.worker_vivo()
        if ativa and ativa["status"] == "na_fila" and not worker:
            self.garantir_worker()
        tem_progresso = any(
            ind.get(chave, 0) > 0
            for chave in ("processados", "falhas", "exportados", "imagens")
        ) or any(STATE.glob("checkpoint_master_lote_*.csv"))
        if ativa is None:
            aplicacao = "ociosa"
        elif ativa["status"] == "na_fila":
            aplicacao = "na fila"
        else:
            aplicacao = "executando"
        return {
            "aplicacao": aplicacao,
            "worker": worker,
            "modo_continuacao": tem_progresso,
            "atualizado_em": datetime.now().isoformat(timespec="seconds"),
            "ultima": ultima,
            "indicadores": ind,
            "funil": fluxo,
            "etapas": self._cartoes(fluxo),
            "execucao": (
                {"id": ativa["id"], "etapa": ativa["etapa"], "status": ativa["status"]}
                if ativa
                else None
            ),
        }

    def resetar(self, dados: dict[str, Any]) -> dict[str, Any]:
        """
        Reinicia os contadores, checkpoints e saídas para recomeçar do zero.
        Exige confirmação explícita e recusa se houver execução em andamento.
        """
        if not isinstance(dados, dict) or dados.get("confirmar") is not True:
            raise ErroPainel(400, "Confirme o reset antes de prosseguir.")
        self.store.marcar_orfas()
        if self.store.execucao_ativa():
            raise ErroPainel(
                409, "Não é possível resetar com uma execução em andamento."
            )
        ensure_dirs()
        for arq in (SKUS_PROCESSADOS, SKUS_SEM_PRECO, LINKS_FILE):
            if arq.exists():
                arq.unlink(missing_ok=True)
        catalogo().resetar()
        if STATE.exists():
            for ckpt in STATE.glob("checkpoint_master_lote_*"):
                if ckpt.is_file():
                    ckpt.unlink(missing_ok=True)
        base_out = OUT.resolve()
        if base_out.exists():
            for item in list(base_out.iterdir()):
                if item.name == ".gitkeep":
                    continue
                if item.is_dir():
                    if item.name == "Planilhas de Categorias":
                        continue
                    shutil.rmtree(item, ignore_errors=True)
                elif item.is_file():
                    item.unlink(missing_ok=True)
        self.store.resetar()
        return {"ok": True, "painel": self.painel()}

    def arquivos(self) -> list[dict[str, Any]]:
        """
        Só os entregáveis de data/out: um arquivo por saída (a cópia mais
        recente), e cada subpasta resumida numa linha.
        """
        base = OUT.resolve()
        if not base.exists():
            return []
        itens = []
        for caminho in base.iterdir():
            resolvido = caminho.resolve()
            if base not in resolvido.parents:
                continue
            if caminho.is_dir():
                itens.append(_resumo_pasta(caminho))
                continue
            if not caminho.is_file() or _arquivo_interno(caminho.name):
                continue
            stat = caminho.stat()
            itens.append(
                {
                    "nome": caminho.name,
                    "tamanho": stat.st_size,
                    "atualizado": datetime.fromtimestamp(stat.st_mtime).isoformat(
                        timespec="seconds"
                    ),
                    "tipo": caminho.suffix.lower().lstrip(".") or "arquivo",
                    "produtos": _produtos_da_saida(caminho),
                }
            )
        itens.sort(key=lambda item: item["atualizado"], reverse=True)
        return itens[:300]

    def arquivo_autorizado(self, nome: str) -> Path:
        if not nome or nome.startswith(("/", "\\")) or ".." in Path(nome).parts:
            raise ErroPainel(400, "Caminho não autorizado.")
        base = OUT.resolve()
        destino = (base / nome).resolve()
        if base not in destino.parents or not destino.is_file():
            raise ErroPainel(404, "Arquivo não encontrado.")
        if destino.stat().st_size > 100 * 1024 * 1024:
            raise ErroPainel(400, "Arquivo acima do limite de 100 MB.")
        return destino

    def _parametros(self, dados: dict[str, Any]) -> dict[str, Any]:
        config = self.configuracao()
        aba = str(dados.get("aba") or config["aba"])
        abas = abas_do_template()
        if abas and aba not in abas:
            raise ErroPainel(400, "Aba do Mercado Livre não existe no template.")
        master = dados.get("master") or ""
        if not isinstance(master, str):
            raise ErroPainel(400, "Master inválido.")
        if master:
            resolver_master(master)
        return {
            "limite": _inteiro(dados.get("limite", config["limite"]), "limite", 1, 5000),
            "lote": _inteiro(dados.get("lote", 1), "lote", 1, 999),
            "timeout": _inteiro(
                dados.get("timeout", config["timeout"]), "timeout", 5, 180
            ),
            "tentativas": _inteiro(
                dados.get("tentativas", config["tentativas"]), "tentativas", 1, 5
            ),
            "aba": aba,
            "master": master,
            "dry_run": _bool(dados.get("dry_run"), "dry_run"),
            "force": _bool(dados.get("force"), "force"),
            "refresh": _bool(dados.get("refresh"), "refresh"),
        }

    def _parametros_gavazzi(self, dados: dict[str, Any]) -> dict[str, Any]:
        planilha = str(dados.get("planilha") or "").strip()
        if not planilha:
            raise ErroPainel(400, "Informe a planilha de preços da Gavazzi.")
        caminho = Path(planilha)
        if not caminho.is_absolute():
            caminho = ROOT / planilha
        if not caminho.is_file():
            raise ErroPainel(400, f"Planilha não encontrada: {planilha}")
        if caminho.suffix.lower() not in (".csv", ".xlsx", ".xls"):
            raise ErroPainel(400, "A planilha deve ser CSV ou XLSX.")
        limite = dados.get("limite")
        return {
            "planilha": str(caminho),
            "limite": _inteiro(limite, "limite", 1, 5000) if limite not in (None, "") else None,
            "sem_ia": _bool(dados.get("sem_ia"), "sem_ia"),
            "dry_run": _bool(dados.get("dry_run"), "dry_run"),
            "refresh_links": _bool(dados.get("refresh_links"), "refresh_links"),
            "coluna_codigo": str(dados.get("coluna_codigo") or "").strip() or None,
            "coluna_preco": str(dados.get("coluna_preco") or "").strip() or None,
        }

    def _parametros_gavazzi_fotos(self, dados: dict[str, Any]) -> dict[str, Any]:
        mapa = str(dados.get("mapa") or "").strip()
        caminho = Path(mapa) if mapa else OUT / "gavazzi-imagens.json"
        if not caminho.is_absolute():
            caminho = ROOT / caminho
        if caminho.resolve().parent != OUT.resolve():
            raise ErroPainel(400, "O mapeamento de imagens deve estar em data/out.")
        if not caminho.is_file():
            raise ErroPainel(
                400,
                "gavazzi-imagens.json não encontrado em data/out. "
                "Rode o Scrap Gavazzi antes de vincular fotos.",
            )
        return {
            "mapa": str(caminho),
            "dry_run": _bool(dados.get("dry_run"), "dry_run"),
            "force": _bool(dados.get("force"), "force"),
            "timeout": _inteiro(dados.get("timeout", 20), "timeout", 5, 180),
        }

    def gavazzi_planilhas(self) -> list[dict[str, Any]]:
        """Planilhas de preços disponíveis em data/in/ para a aba Gavazzi."""
        base = ROOT / "data" / "in"
        if not base.exists():
            return []
        itens = []
        for arq in sorted(base.iterdir()):
            if arq.is_file() and arq.suffix.lower() in (".csv", ".xlsx", ".xls"):
                stat = arq.stat()
                itens.append({
                    "nome": f"data/in/{arq.name}",
                    "tamanho": stat.st_size,
                    "atualizado": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                })
        return itens

    def gavazzi_outputs(self) -> list[dict[str, Any]]:
        """Saídas gavazzi-* em data/out (inclusive as carimbadas, ocultas no /api/arquivos)."""
        base = OUT.resolve()
        if not base.exists():
            return []
        itens = []
        for arq in sorted(base.glob("gavazzi-*")):
            if not arq.is_file() or arq.suffix == ".tmp":
                continue
            stat = arq.stat()
            itens.append({
                "nome": arq.name,
                "tamanho": stat.st_size,
                "atualizado": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "tipo": arq.suffix.lower().lstrip(".") or "arquivo",
            })
        itens.sort(key=lambda i: i["atualizado"], reverse=True)
        return itens

    def _parametros_tax(self, operacao: str, dados: dict[str, Any]) -> dict[str, Any]:
        """Validação síncrona das ações do Motor de Categorias (a execução vai para a fila)."""
        params: dict[str, Any] = {}
        if operacao in ("tax-atribuir", "tax-desfazer", "tax-arquivar"):
            filtro = dados.get("filtro")
            ids = dados.get("ids")
            if filtro is not None:
                if not isinstance(filtro, dict):
                    raise ErroPainel(400, "Filtro inválido.")
                params["filtro"] = {k: filtro.get(k) for k in
                                    ("status", "origem", "busca", "conf_min", "conf_max", "sugestao")}
            else:
                if not isinstance(ids, list) or not ids or len(ids) > MAX_IDS_LOTE:
                    raise ErroPainel(400, "Selecione ao menos um produto.")
                try:
                    params["ids"] = [int(i) for i in ids]
                except (TypeError, ValueError) as exc:
                    raise ErroPainel(400, "IDs de produto inválidos.") from exc
        if operacao == "tax-atribuir":
            params["usar_sugestao"] = _bool(dados.get("usar_sugestao"), "usar_sugestao")
            cat = dados.get("categoria_id")
            if not params["usar_sugestao"]:
                from wago.dashboard.taxonomia import categorias_finais

                if cat not in {c["id"] for c in categorias_finais()}:
                    raise ErroPainel(400, "Escolha uma categoria final válida.")
                params["categoria_id"] = cat
        elif operacao == "tax-atribuir-bling":
            # Bling grava direto na API: seleção por ids ou por filtro; categoria
            # fixa (id_alvo v8) ou a sugestão individual de cada produto.
            params["usar_sugestao"] = _bool(dados.get("usar_sugestao"), "usar_sugestao")
            filtro = dados.get("filtro")
            if filtro is not None:
                if not isinstance(filtro, dict):
                    raise ErroPainel(400, "Filtro inválido.")
                params["filtro"] = {k: filtro.get(k) for k in
                                    ("status", "busca", "conf_min", "conf_max", "ocultar")}
            else:
                ids = dados.get("ids")
                if not isinstance(ids, list) or not ids or len(ids) > MAX_IDS_LOTE:
                    raise ErroPainel(400, "Selecione ao menos um produto.")
                try:
                    params["ids"] = [int(i) for i in ids]
                except (TypeError, ValueError) as exc:
                    raise ErroPainel(400, "IDs de produto inválidos.") from exc
            if not params["usar_sugestao"]:
                id_alvo = dados.get("id_alvo")
                if id_alvo is None:
                    raise ErroPainel(400, "Selecione uma categoria.")
                params["id_alvo"] = str(id_alvo)
        elif operacao == "tax-arquivar":
            params["marcar"] = _bool(dados.get("marcar"), "marcar", True)
            if dados.get("motivo") == "falta_informacao":
                params["motivo"] = "falta_informacao"
        elif operacao in ("tax-classificar", "tax-classificar-bling"):
            params["sem_ia"] = _bool(dados.get("sem_ia"), "sem_ia")
            if not params["sem_ia"]:
                from wago.taxonomia.ia import obter_provedor

                if obter_provedor() is None:
                    raise ErroPainel(
                        400,
                        "IA não configurada: defina GEMINI_API_KEY ou OPENAI_API_KEY "
                        "no .env, ou rode Classificar (regras).",
                    )
        elif operacao == "tax-proposta":
            if dados.get("decisao") not in ("aprovar", "rejeitar") or not isinstance(dados.get("chave"), str):
                raise ErroPainel(400, "Proposta ou decisão inválida.")
            params.update({k: dados.get(k) for k in ("chave", "decisao", "nome", "pai", "existente")})
            for k in ("nome", "pai", "existente"):
                if params[k] is not None and not isinstance(params[k], str):
                    raise ErroPainel(400, f"{k} inválido")
        elif operacao == "tax-aplicar-bling":
            for chave, padrao, minimo, maximo in (("limite_diario", 100_000, 1, 120_000), ("tamanho_lote", 50, 1, 1000), ("pausa_lote", 30, 0, 3600)):
                valor = dados.get(chave, padrao)
                if isinstance(valor, float) and not valor.is_integer():
                    raise ErroPainel(400, f"{chave} deve ser inteiro")
                params[chave] = _inteiro(valor, chave, minimo, maximo)
        elif operacao == "tax-aprovar-taxonomia":
            por = str(dados.get("por") or "").strip()
            if not por or len(por) > 60:
                raise ErroPainel(400, "Informe o nome de quem aprova.")
            params["por"] = por
        return params

    def _cartoes(self, fluxo: dict[str, int] | None = None) -> list[dict[str, Any]]:
        fluxo = fluxo or {}
        acumulados = {
            "discover": fluxo.get("total"),
            "scrape": fluxo.get("processados"),
            "fill-nuvemshop": fluxo.get("no_csv"),
            "images": fluxo.get(EXPORTADO),
        }
        ultimas = self.store.ultimas_etapas()
        cartoes = []
        for etapa in ETAPAS_PAINEL:
            info = ultimas.get(etapa)
            status = info["status"] if info else "aguardando"
            msg_bruta = str(info.get("mensagem") or "").strip() if info else ""
            if len(msg_bruta) > 90:
                msg_curta = msg_bruta[:87].rstrip() + "..."
            else:
                msg_curta = msg_bruta
            cartoes.append(
                {
                    "id": etapa,
                    "nome": NOMES[etapa],
                    "status": status,
                    "quando": info.get("fim") or info.get("inicio") if info else None,
                    "duracao_s": _duracao(info),
                    "registros": info.get("registros") if info else None,
                    "acumulado": acumulados.get(etapa),
                    "mensagem": msg_curta,
                }
            )
        return cartoes


def _duracao(info: dict[str, Any] | None) -> int | None:
    if not info or not info.get("inicio") or not info.get("fim"):
        return None
    try:
        inicio = datetime.fromisoformat(info["inicio"])
        fim = datetime.fromisoformat(info["fim"])
    except ValueError:
        return None
    return max(int((fim - inicio).total_seconds()), 0)


def listar_masters() -> list[dict[str, str]]:
    base = OUT.resolve()
    if not base.exists():
        return []
    itens = []
    for caminho in base.glob("*master*.csv"):
        resolvido = caminho.resolve()
        if not resolvido.is_file() or base not in resolvido.parents:
            continue
        if "master" not in resolvido.name.lower():
            continue
        itens.append(
            {
                "nome": resolvido.name,
                "atualizado": datetime.fromtimestamp(
                    resolvido.stat().st_mtime
                ).isoformat(timespec="seconds"),
            }
        )
    itens.sort(key=lambda item: item["atualizado"], reverse=True)
    return itens


def resolver_master(nome: str) -> Path:
    if not nome or Path(nome).name != nome or nome in {".", ".."}:
        raise ErroPainel(400, "Master fora dos arquivos autorizados.")
    base = OUT.resolve()
    destino = (base / nome).resolve()
    if base not in destino.parents or not destino.is_file():
        raise ErroPainel(400, "Master fora dos arquivos autorizados.")
    if destino.suffix.lower() != ".csv" or "master" not in destino.name.lower():
        raise ErroPainel(400, "Selecione um arquivo master.csv de data/out.")
    return destino


def master_util(nome: str) -> Path | None:
    if nome:
        caminho = resolver_master(nome)
    else:
        masters = listar_masters()
        if not masters:
            return None
        caminho = resolver_master(masters[0]["nome"])
    return caminho if _tem_produtos(caminho) else None


def _tem_produtos(caminho: Path) -> bool:
    try:
        linhas = [
            linha
            for linha in caminho.read_text(encoding="utf-8-sig").splitlines()
            if linha.strip()
        ]
    except OSError:
        return False
    return len(linhas) >= 2


def _nomes_saida() -> set[str]:
    base = OUT.resolve()
    if not base.exists():
        return set()
    # os.walk não segue links simbólicos de diretório: tudo fica dentro de data/out sem resolve() por arquivo
    nomes = set()
    for raiz, _dirs, arquivos in os.walk(base):
        rel = Path(raiz).relative_to(base)
        for nome in arquivos:
            if not os.path.islink(os.path.join(raiz, nome)):
                nomes.add((rel / nome).as_posix())
    return nomes


def _contar_linhas(caminho: Path) -> int | None:
    if not caminho.exists():
        return 0
    try:
        with caminho.open("r", encoding="utf-8-sig", newline="") as fh:
            linhas = [
                row
                for row in csv.reader(fh, delimiter=";")
                if any(str(c).strip() for c in row)
            ]
    except OSError:
        return None
    return max(len(linhas) - 1, 0)


def _produtos_no_arquivo(caminho: Path) -> int | None:
    if caminho.name.endswith(".resumo.json"):
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if "exportados" in dados:
            return int(dados["exportados"])
        if "baixadas" in dados:
            return int(dados["baixadas"])
    if caminho.suffix.lower() == ".csv" and caminho.stat().st_size < 5_000_000:
        return _contar_linhas(caminho)
    return None


_CARIMBO = re.compile(r"_\d{2}-\d{2}_\d{6}(\.resumo)?\.\w+$")
_PREFIXOS_INTERNOS = ("wago_nuvemshop_lote_", "wago_nuvemshop_checkpoint_")
_EXT_IMAGEM = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _arquivo_interno(nome: str) -> bool:
    """Histórico com carimbo de hora, resumos, parciais do scrape e temporários."""
    return (
        nome.startswith(".")
        or nome.endswith((".tmp", ".resumo.json"))
        or nome.startswith(_PREFIXOS_INTERNOS)
        or bool(_CARIMBO.search(nome))
    )


def _produtos_da_saida(caminho: Path) -> int | None:
    """XLSX não tem contagem barata: usa o resumo da versão carimbada mais recente."""
    if caminho.suffix.lower() == ".xlsx":
        resumos = sorted(
            caminho.parent.glob(f"{caminho.stem}_*.resumo.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return _produtos_no_arquivo(resumos[0]) if resumos else None
    return _produtos_no_arquivo(caminho)


def _resumo_pasta(pasta: Path) -> dict[str, Any]:
    tamanho = 0
    recente = pasta.stat().st_mtime
    arquivos = 0
    for arq in pasta.rglob("*"):
        if arq.is_file():
            stat = arq.stat()
            tamanho += stat.st_size
            recente = max(recente, stat.st_mtime)
            arquivos += 1
    produtos = None
    if pasta.name == "imagens":
        produtos = sum(
            1
            for sub in pasta.iterdir()
            if sub.is_dir() and any(a.suffix.lower() in _EXT_IMAGEM for a in sub.iterdir())
        )
    return {
        "nome": f"{pasta.name}/",
        "tamanho": tamanho,
        "atualizado": datetime.fromtimestamp(recente).isoformat(timespec="seconds"),
        "tipo": "pasta",
        "arquivos": arquivos,
        "produtos": produtos,
    }


def indicadores(contagem: dict[str, int]) -> dict[str, int]:
    ensure_dirs()
    fluxo = funil()
    exportados = 0
    preenchido = OUT / "produtos-Nuvemshop-preenchido.csv"
    if preenchido.exists():
        exportados = _contar_linhas(preenchido) or 0
    imagens = 0
    pasta = OUT / "imagens"
    if pasta.exists():
        for arquivo in pasta.rglob("*"):
            if arquivo.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                imagens += 1
    concluidas = contagem.get("concluida", 0) + contagem.get("concluida_com_avisos", 0)
    return {
        "descobertos": fluxo["total"],
        "processados": fluxo["processados"],
        "falhas": fluxo[FILTRADO_SEM_CUSTO],
        "exportados": exportados,
        "imagens": imagens,
        "execucoes_concluidas": concluidas,
        "execucoes_falhas": contagem.get("falha", 0),
    }


def funil() -> dict[str, int]:
    """Quantos SKUs do catálogo estão em cada status. Cada SKU conta uma vez."""
    dados = catalogo().contagem()
    dados["processados"] = sum(dados[s] for s in POS_SCRAPE)
    dados["no_csv"] = dados[AGUARDANDO_IMAGEM] + dados[EXPORTADO]
    return dados


def iniciar_worker_destacado() -> None:
    """Sobe `python -m wago worker` fora do processo do painel."""
    ensure_dirs()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    # Console oculto próprio: o Playwright herda e não abre janela; grupo novo
    # isola do Ctrl+C do painel; breakaway tira do job do terminal quando permitido.
    tentativas: list[dict[str, Any]] = [{"start_new_session": True}]
    if sys.platform == "win32":
        base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        tentativas = [
            {"creationflags": base | subprocess.CREATE_BREAKAWAY_FROM_JOB},
            {"creationflags": base},
        ]
    with (STATE / "worker.log").open("a", encoding="utf-8") as log:
        for i, opcoes in enumerate(tentativas):
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "wago", "worker"],
                    cwd=str(ROOT),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    **opcoes,
                )
                return
            except OSError:
                if i == len(tentativas) - 1:
                    raise


def _acao_gavazzi_scrap(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    if cancelado():
        raise Cancelado()
    from wago.config import load_env
    from wago.gavazzi.pipeline import rodar

    load_env()
    res = rodar(
        params["planilha"],
        saida=OUT,
        limit=params.get("limite"),
        sem_ia=bool(params.get("sem_ia")),
        refresh_links=bool(params.get("refresh_links")),
        dry_run=bool(params.get("dry_run")),
        coluna_codigo=params.get("coluna_codigo"),
        coluna_preco=params.get("coluna_preco"),
        cancelado=cancelado,
    )
    return res["processados"]


def _acao_gavazzi_fotos(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    if cancelado():
        raise Cancelado()
    from wago.gavazzi.photos_linker import vincular_fotos_gavazzi

    resumo = vincular_fotos_gavazzi(
        params.get("mapa"),
        dry_run=bool(params.get("dry_run")),
        force=bool(params.get("force")),
        timeout=int(params.get("timeout") or 20),
        cancelado=cancelado,
    )
    return int(resumo["sucessos"])


def _acao_discover(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    if cancelado():
        raise Cancelado()
    from wago.discover import carregar_links

    links = carregar_links(force_refresh=bool(params["refresh"]))
    if cancelado():
        raise Cancelado()
    return len(links or [])


def _acao_scrape(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    from wago.config import load_env
    from wago.scrape import rodar_scraper

    load_env()

    produtos = rodar_scraper(
        limite_lote=params["limite"],
        lote_numero=params["lote"],
        force_refresh_links=bool(params["refresh"]),
        headless=True,
        cancelado=cancelado,
    )
    return len(produtos)


def _acao_export(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    if cancelado():
        raise Cancelado()
    from wago.export import exportar_de_master

    master = master_util(params.get("master") or "")
    if master is None:
        raise ErroPainel(400, "Master válido ausente.")
    exportar_de_master(master, lote_numero=params["lote"])
    return _contar_linhas(master) or 0


def _acao_nuvemshop(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    if cancelado():
        raise Cancelado()
    from wago.fill_nuvemshop import fill_nuvemshop

    master = master_util(params.get("master") or "")
    if master is None:
        raise ErroPainel(400, "Master válido ausente.")
    destino = fill_nuvemshop(
        master, dry_run=params["dry_run"], force=params["force"]
    )
    if destino is None:
        return 0
    resumo = destino.with_suffix(".resumo.json")
    if resumo.exists():
        dados = json.loads(resumo.read_text(encoding="utf-8"))
        return int(dados.get("exportados") or 0)
    return _contar_linhas(destino) or 0


def _acao_imagens(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    from wago.images import baixar_imagens_lote

    master = master_util(params.get("master") or "")
    if master is None:
        raise ErroPainel(400, "Master válido ausente.")
    baixar_imagens_lote(
        master,
        limit=None,
        timeout=params["timeout"],
        tentativas=params["tentativas"],
        dry_run=params["dry_run"],
        cancelado=cancelado,
    )
    resumo = OUT / "imagens" / "resumo-imagens.json"
    if resumo.exists() and not params["dry_run"]:
        dados = json.loads(resumo.read_text(encoding="utf-8"))
        return int(dados.get("baixadas") or 0) + int(dados.get("ignoradas") or 0)
    return 0


def _acao_ml(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    if cancelado():
        raise Cancelado()
    import importlib
    import wago.fill_ml as _fill_ml_mod

    importlib.reload(_fill_ml_mod)

    master = master_util(params.get("master") or "")
    if master is None:
        raise ErroPainel(400, "Master válido ausente.")
    destino = _fill_ml_mod.fill_ml(
        master,
        sheet=params["aba"],
        dry_run=params["dry_run"],
        force=params["force"],
    )
    if destino is None:
        return 0
    resumo = destino.with_suffix(".resumo.json")
    if resumo.exists():
        dados = json.loads(resumo.read_text(encoding="utf-8"))
        return int(dados.get("exportados") or 0)
    return 0


def _acao_photos(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    if cancelado():
        raise Cancelado()
    from wago.photos import _resolver_csv, vincular_fotos_lote

    nome = params.get("master") or ""
    master = resolver_master(nome) if nome else _resolver_csv(None)
    print(f"Fotos Nuvemshop: arquivo de entrada selecionado: {master.name}", flush=True)
    return vincular_fotos_lote(
        master,
        dry_run=params["dry_run"],
        force=params["force"],
        timeout=params["timeout"],
        cancelado=cancelado,
    )


def _tax(funcao_nome: str) -> Callable[[dict[str, Any], Callable[[], bool]], int]:
    """Adapta uma ação do Motor de Categorias para a fila (erros viram ErroPainel)."""

    def acao(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
        from wago.dashboard import taxonomia as tx

        if cancelado():
            raise Cancelado()
        try:
            return int(getattr(tx, funcao_nome)(params) or 0)
        except tx.ErroTaxonomia as exc:
            raise ErroPainel(exc.status, exc.mensagem) from exc

    return acao


def _acao_tax_inventario(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    from wago.config import load_env
    from wago.taxonomia.inventario import carregar_snapshot, executar_inventario

    load_env()
    saida = executar_inventario(cancelado=cancelado)
    return len(carregar_snapshot(saida["snapshot"])["produtos"])


def _acao_tax_classificar(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    from wago.config import load_env
    from wago.dashboard.taxonomia import ESTADO_IA
    from wago.taxonomia.classificador import executar_classificacao

    load_env()
    ESTADO_IA["aviso"] = None
    try:
        saida = executar_classificacao(
            sem_ia=params["sem_ia"], cancelado=cancelado,
            aviso_cota=lambda info: ESTADO_IA.update(aviso=info),
        )
    finally:
        ESTADO_IA["aviso"] = None
    if cancelado() or not saida:
        raise Cancelado()
    dados = json.loads(Path(saida["json"]).read_text(encoding="utf-8"))
    return int(dados["resumo"]["categorizados_auto"])


def _acao_tax_classificar_bling(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    from wago.config import load_env
    from wago.dashboard.taxonomia import ESTADO_IA
    from wago.taxonomia.classificador import executar_classificacao_bling

    load_env()
    ESTADO_IA["aviso"] = None
    try:
        saida = executar_classificacao_bling(
            sem_ia=params["sem_ia"], cancelado=cancelado,
            aviso_cota=lambda info: ESTADO_IA.update(aviso=info),
        )
    finally:
        ESTADO_IA["aviso"] = None
    if cancelado() or not saida:
        raise Cancelado()
    dados = json.loads(Path(saida["json"]).read_text(encoding="utf-8"))
    return int(dados["resumo"]["categorizados_auto"])


def _acao_tax_plano(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    from wago.config import load_env
    from wago.taxonomia.plano import executar_plano_dry_run

    load_env()
    if cancelado():
        raise Cancelado()
    saida = executar_plano_dry_run(log=print, verboso=False)
    dados = json.loads(Path(saida["json"]).read_text(encoding="utf-8"))
    return int(dados["resumo"]["total_acoes"])


def _acao_tax_aplicar(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    from wago.config import load_env
    from wago.taxonomia.aplicar import aplicar_plano_atual

    load_env()
    if cancelado():
        raise Cancelado()
    destino = aplicar_plano_atual(log=print, cancelado=cancelado)
    diario = json.loads(Path(destino).read_text(encoding="utf-8"))
    if diario.get("status") == "cancelada":
        raise Cancelado()
    return int(diario.get("ok") or 0)


def _acao_tax_plano_bling(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    from wago.config import load_env
    from wago.taxonomia.plano import executar_plano_dry_run_bling

    load_env()
    if cancelado():
        raise Cancelado()
    saida = executar_plano_dry_run_bling(log=print, verboso=False)
    dados = json.loads(Path(saida["json"]).read_text(encoding="utf-8"))
    return int(dados["resumo"]["total_acoes"])


def _acao_tax_inventario_bling(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    from datetime import datetime
    from wago.config import load_env
    from wago.marketplaces import criar_provider
    from wago.taxonomia import modelo
    from wago.taxonomia.confirmacoes_bling import reconciliar
    from wago.taxonomia.inventario import salvar_snapshot

    load_env()
    if cancelado():
        raise Cancelado()
    provider = criar_provider("bling")
    categorias = provider.listar_categorias()
    produtos = []
    for produto in provider.listar_produtos():
        if cancelado():
            raise Cancelado()
        produtos.append(produto)
    if cancelado():
        raise Cancelado()
    agora = datetime.now()
    snapshot = {"gerado_em": agora.isoformat(timespec="seconds"), "categorias": categorias, "produtos": produtos}
    destino = modelo.TAXONOMIA / "normalizados" / f"snapshot-bling-normalizado-{agora.strftime('%Y%m%dT%H%M%S%f')}.json"
    salvar_snapshot(snapshot, destino)
    reconciliar(produtos)
    print(f"Inventário Bling atualizado: {len(produtos)} produto(s). Atribuições reconciliadas.")
    return len(produtos)


def _acao_tax_aplicar_bling(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    from wago.config import load_env
    from wago.taxonomia import modelo
    from wago.taxonomia.aplicar import carregar_diario, pasta_aplicacoes, tax_aplicar_provider

    load_env()
    if cancelado():
        raise Cancelado()
    # Retomada automática: se o diário mais recente do plano atual ficou
    # incompleto (cancelada/em_andamento/interrompida), continua dele —
    # ações 'ok' são puladas e falhas reexecutadas.
    retomar = None
    planos = sorted((modelo.TAXONOMIA / "planos").glob("plano-*.json"), key=lambda p: p.stat().st_mtime)
    if planos:
        for cand in sorted(pasta_aplicacoes().glob("aplicacao-*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                anterior = carregar_diario(cand)
            except Exception:
                continue
            if (anterior.get("provider") == "bling"
                    and anterior.get("plano") == planos[-1].name
                    and anterior.get("status") in ("cancelada", "em_andamento", "interrompida", "pausada")):
                retomar = cand
                break
    if retomar:
        print(f"Retomando aplicação anterior: {retomar.name}")
    destino = tax_aplicar_provider(
        "bling", log=print, retomar=retomar, cancelado=cancelado,
        limite_diario=params.get("limite_diario", 100_000),
        tamanho_lote=params.get("tamanho_lote", 50), pausa_lote=params.get("pausa_lote", 30),
    )
    diario = json.loads(Path(destino).read_text(encoding="utf-8"))
    if diario.get("status") == "cancelada":
        raise Cancelado()
    if diario.get("status") == "pausada":
        raise Pausado(
            "Cota diária atingida. O progresso foi salvo. Volte amanhã para retomar.",
            registros=int(diario.get("ok") or 0),
        )
    return int(diario.get("ok") or 0)


def _acao_tax_atribuir_bling(params: dict[str, Any], cancelado: Callable[[], bool]) -> int:
    """Atribui categoria a produtos **sem chamar a API do Bling**.

    Segue o padrão da Nuvemshop: grava decisões (produto → id_alvo) em
    `decisoes.json`, que o `tax-plano-bling` converte em ações
    `mover_produto`. A escrita real acontece depois, em "Aplicar Plano",
    com micro-lotes, cota diária e retomada — nunca em rajada direta.
    """
    from wago.config import load_env
    from wago.dashboard import taxonomia as tx
    from wago.taxonomia.classificador import arq_decisoes, registro_decisao

    load_env()
    if cancelado():
        raise Cancelado()

    mapping_path = Path("data/state/bling_mapping.json")
    with open(mapping_path, encoding="utf-8") as f:
        mapping = json.load(f)

    if params.get("filtro") is not None:
        ids = tx.bling_ids_por_filtro(params["filtro"])
    else:
        ids = [int(i) for i in params.get("ids", [])]
    if not ids:
        raise ErroPainel(400, "Selecione ao menos um produto.")
    if len(ids) > MAX_IDS_LOTE:
        raise ErroPainel(400, f"Seleção acima do limite de {MAX_IDS_LOTE} produtos.")

    # Resolve cada produto para o id_alvo (slug v8) — mesma lógica de antes.
    alvo_por_produto: dict[int, str] = {}
    sem_alvo: list[int] = []
    if params.get("usar_sugestao"):
        alvos = tx.bling_alvos_por_produto()
        for pid in ids:
            alvo = alvos.get(str(pid))
            if alvo:
                alvo_por_produto[pid] = alvo
            else:
                sem_alvo.append(pid)
    else:
        alvo = params.get("id_alvo")
        if not alvo:
            raise ErroPainel(400, "Selecione uma categoria.")
        if str(alvo) not in mapping:
            raise ErroPainel(400, f"Categoria {alvo} sem mapping Bling.")
        for pid in ids:
            alvo_por_produto[pid] = str(alvo)

    decisoes_path = arq_decisoes()
    bruto = {}
    if decisoes_path.exists():
        try:
            bruto = json.loads(decisoes_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bruto = {}
    for pid, alvo in alvo_por_produto.items():
        bruto[str(pid)] = registro_decisao(alvo)
    tmp = decisoes_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(bruto, ensure_ascii=False, indent=1), encoding="utf-8")
    from wago.taxonomia.motor_universal import replace_robusto
    replace_robusto(tmp, decisoes_path)

    if sem_alvo:
        print(f"{len(sem_alvo)} produto(s) sem categoria sugerida; pulados.", flush=True)
    print(f"{len(alvo_por_produto)} decisão(ões) registrada(s) no plano. "
          "Gere o Plano de Mudanças e aplique com as regras de cota.", flush=True)
    return len(alvo_por_produto)


ACOES_REAIS = {
    "tax-inventario": _acao_tax_inventario,
    "tax-inventario-bling": _acao_tax_inventario_bling,
    "tax-classificar": _acao_tax_classificar,
    "tax-classificar-bling": _acao_tax_classificar_bling,
    "tax-atribuir": _tax("acao_atribuir"),
    "tax-atribuir-bling": _acao_tax_atribuir_bling,
    "tax-desfazer": _tax("acao_desfazer"),
    "tax-arquivar": _tax("acao_arquivar"),
    "tax-proposta": _tax("acao_proposta"),
    "tax-aprovar-taxonomia": _tax("acao_aprovar_taxonomia"),
    "tax-plano": _acao_tax_plano,
    "tax-aplicar": _acao_tax_aplicar,
    "tax-plano-bling": _acao_tax_plano_bling,
    "tax-aplicar-bling": _acao_tax_aplicar_bling,
    "discover": _acao_discover,
    "scrape": _acao_scrape,
    "export": _acao_export,
    "fill-nuvemshop": _acao_nuvemshop,
    "images": _acao_imagens,
    "fill-ml": _acao_ml,
    "photos": _acao_photos,
    "gavazzi-scrap": _acao_gavazzi_scrap,
    "gavazzi-vincular-fotos": _acao_gavazzi_fotos,
}
