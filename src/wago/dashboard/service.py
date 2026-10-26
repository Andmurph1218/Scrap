"""Executa o pipeline a partir de uma lista fixa de operações."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import uuid
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

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

OPERACOES = (
    "discover",
    "scrape",
    "export",
    "fill-nuvemshop",
    "images",
    "fill-ml",
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
EXPORTERS = {"export", "fill-nuvemshop", "images", "fill-ml"}
NOMES = {
    "discover": "Discover links",
    "scrape": "Scrape autenticado",
    "export": "Export master",
    "fill-nuvemshop": "Preencher Nuvemshop",
    "images": "Baixar imagens",
    "fill-ml": "Preencher Mercado Livre",
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
    def __init__(self, store: Store, acoes: dict[str, Callable] | None = None):
        self.store = store
        self.acoes = acoes or ACOES_REAIS
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._execucao_id: str | None = None
        self._etapa_atual = ""

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
                for chave in ORDEM_PIPELINE
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
        if not self._lock.acquire(blocking=False):
            raise ErroPainel(409, "Já existe uma execução em andamento.")
        execucao_id = uuid.uuid4().hex[:12]
        self._cancel.clear()
        self._execucao_id = execucao_id
        self.store.criar_execucao(
            execucao_id, operacao, json.dumps(params, ensure_ascii=False)
        )
        self._thread = threading.Thread(
            target=self._trabalhar,
            args=(execucao_id, operacao, params),
            name=f"wago-{execucao_id}",
            daemon=True,
        )
        try:
            self._thread.start()
        except Exception:
            self._execucao_id = None
            self._lock.release()
            raise
        return {"id": execucao_id, "status": "executando"}

    def cancelar(self, execucao_id: str) -> dict[str, str]:
        if self._execucao_id != execucao_id or not self._lock.locked():
            raise ErroPainel(404, "Essa execução não está em andamento.")
        self._cancel.set()
        self.store.evento(
            execucao_id,
            self._etapa_atual or "painel",
            "aviso",
            "Cancelamento pedido. A gravação atual termina antes de parar.",
        )
        return {"id": execucao_id, "status": "cancelando"}

    def painel(self) -> dict[str, Any]:
        execucoes = self.store.listar_execucoes(1)
        ultima = execucoes[0] if execucoes else None
        contagem = self.store.contar_status()
        ind = indicadores(contagem)
        tem_progresso = any(
            ind.get(chave, 0) > 0
            for chave in ("processados", "falhas", "exportados", "imagens")
        ) or any(STATE.glob("checkpoint_master_lote_*.csv"))
        return {
            "aplicacao": "executando" if self._lock.locked() else "ociosa",
            "modo_continuacao": tem_progresso,
            "atualizado_em": datetime.now().isoformat(timespec="seconds"),
            "ultima": ultima,
            "indicadores": ind,
            "etapas": self._cartoes(),
            "execucao": (
                {"id": self._execucao_id, "etapa": self._etapa_atual}
                if self._lock.locked()
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
        if not self._lock.acquire(blocking=False):
            raise ErroPainel(
                409, "Não é possível resetar com uma execução em andamento."
            )
        try:
            ensure_dirs()
            for arq in (SKUS_PROCESSADOS, SKUS_SEM_PRECO, LINKS_FILE):
                if arq.exists():
                    arq.unlink(missing_ok=True)
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
        finally:
            self._lock.release()
        return {"ok": True, "painel": self.painel()}

    def arquivos(self) -> list[dict[str, Any]]:
        base = OUT.resolve()
        if not base.exists():
            return []
        itens = []
        for caminho in base.rglob("*"):
            if not caminho.is_file():
                continue
            resolvido = caminho.resolve()
            if base not in resolvido.parents:
                continue
            stat = caminho.stat()
            relativo = resolvido.relative_to(base).as_posix()
            itens.append(
                {
                    "nome": relativo,
                    "tamanho": stat.st_size,
                    "atualizado": datetime.fromtimestamp(stat.st_mtime).isoformat(
                        timespec="seconds"
                    ),
                    "tipo": caminho.suffix.lower().lstrip(".") or "arquivo",
                    "produtos": _produtos_no_arquivo(caminho),
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

    def _cartoes(self) -> list[dict[str, Any]]:
        ultimas = self.store.ultimas_etapas()
        cartoes = []
        for etapa in ORDEM_PIPELINE:
            info = ultimas.get(etapa)
            if self._lock.locked() and self._etapa_atual == etapa:
                status = "executando"
            elif info:
                status = info["status"]
            else:
                status = "aguardando"
            cartoes.append(
                {
                    "id": etapa,
                    "nome": NOMES[etapa],
                    "status": status,
                    "quando": info.get("fim") or info.get("inicio") if info else None,
                    "duracao_s": _duracao(info),
                    "registros": info.get("registros") if info else None,
                    "mensagem": info.get("mensagem") if info else "",
                }
            )
        return cartoes

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
                    mensagem = (
                        "Exportadores não rodaram: ainda não há master válido."
                    )
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
            self._execucao_id = None
            self._lock.release()

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
            if self._cancel.is_set():
                raise Cancelado()
            if coletor.erros:
                status = "concluida_com_avisos"
            elif coletor.avisos:
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
        linhas = [
            linha
            for linha in caminho.read_text(encoding="utf-8-sig").splitlines()
            if linha.strip()
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


def indicadores(contagem: dict[str, int]) -> dict[str, int]:
    ensure_dirs()
    descobertos = 0
    if LINKS_FILE.exists():
        try:
            dados = json.loads(LINKS_FILE.read_text(encoding="utf-8"))
            if isinstance(dados, list):
                descobertos = len(dados)
        except (OSError, json.JSONDecodeError):
            descobertos = 0
    processados = _linhas_texto(SKUS_PROCESSADOS)
    falhas = _linhas_texto(SKUS_SEM_PRECO)
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
        "descobertos": descobertos,
        "processados": processados,
        "falhas": falhas,
        "exportados": exportados,
        "imagens": imagens,
        "execucoes_concluidas": concluidas,
        "execucoes_falhas": contagem.get("falha", 0),
    }


def _linhas_texto(caminho: Path) -> int:
    if not caminho.exists():
        return 0
    return sum(1 for linha in caminho.read_text(encoding="utf-8").splitlines() if linha.strip())


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
    from wago.fill_ml import fill_ml

    master = master_util(params.get("master") or "")
    if master is None:
        raise ErroPainel(400, "Master válido ausente.")
    destino = fill_ml(
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


ACOES_REAIS = {
    "discover": _acao_discover,
    "scrape": _acao_scrape,
    "export": _acao_export,
    "fill-nuvemshop": _acao_nuvemshop,
    "images": _acao_imagens,
    "fill-ml": _acao_ml,
}
