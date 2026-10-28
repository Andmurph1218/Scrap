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
}
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
)


class ErroPainel(Exception):
    def __init__(self, status: int, mensagem: str):
        super().__init__(mensagem)
        self.status = status
        self.mensagem = mensagem


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
    nomes = set()
    for caminho in base.rglob("*"):
        if not caminho.is_file():
            continue
        resolvido = caminho.resolve()
        if base in resolvido.parents:
            nomes.add(resolvido.relative_to(base).as_posix())
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
    from wago.photos import vincular_fotos_lote

    master = master_util(params.get("master") or "")
    if master is None:
        raise ErroPainel(400, "Master válido ausente.")
    return vincular_fotos_lote(
        master,
        dry_run=params["dry_run"],
        force=params["force"],
        timeout=params["timeout"],
        cancelado=cancelado,
    )


ACOES_REAIS = {
    "discover": _acao_discover,
    "scrape": _acao_scrape,
    "export": _acao_export,
    "fill-nuvemshop": _acao_nuvemshop,
    "images": _acao_imagens,
    "fill-ml": _acao_ml,
    "photos": _acao_photos,
}
