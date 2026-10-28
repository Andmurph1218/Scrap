"""Servidor local do painel. Escuta apenas em 127.0.0.1."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from wago.config import OUT, STATE, ensure_dirs
from wago.dashboard.service import ErroPainel, Painel
from wago.dashboard.store import Store

PAGINA = Path(__file__).resolve().parent / "static" / "index.html"
_ID = re.compile(r"^[0-9a-f]{12}$")


def abrir_pasta_saida() -> None:
    pasta = OUT.resolve()
    pasta.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(pasta))  # noqa: S606 — só a pasta de saída do projeto
        return
    comando = ["open", str(pasta)] if sys.platform == "darwin" else ["xdg-open", str(pasta)]
    subprocess.run(comando, check=False)


class _ServidorLocal(ThreadingHTTPServer):
    """Escuta exclusiva. No Windows, sem isso outro processo reutiliza a porta."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def criar_servidor(painel: Painel, porta: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, formato: str, *args) -> None:
            return

        def do_GET(self) -> None:
            self._tratar("GET")

        def do_POST(self) -> None:
            self._tratar("POST")

        def _tratar(self, metodo: str) -> None:
            try:
                self._rota(metodo)
            except ErroPainel as exc:
                self._json(exc.status, {"erro": exc.mensagem})
            except Exception as exc:
                self._json(500, {"erro": f"Falha interna: {type(exc).__name__}"})

        def _rota(self, metodo: str) -> None:
            partes = urlparse(self.path)
            caminho = partes.path.rstrip("/") or "/"
            consulta = parse_qs(partes.query)
            if metodo == "GET" and caminho == "/":
                self._html()
                return
            if metodo == "GET" and caminho == "/api/painel":
                self._json(200, painel.painel())
                return
            if metodo == "GET" and caminho == "/api/opcoes":
                self._json(200, painel.opcoes())
                return
            if metodo == "GET" and caminho == "/api/config":
                self._json(200, painel.configuracao())
                return
            if metodo == "POST" and caminho == "/api/config":
                self._json(200, painel.gravar_configuracao(self._corpo()))
                return
            if metodo == "GET" and caminho == "/api/eventos":
                self._json(
                    200,
                    {
                        "eventos": painel.store.listar_eventos(
                            execucao_id=_um(consulta, "execucao"),
                            etapa=_um(consulta, "etapa"),
                            nivel=_um(consulta, "nivel"),
                        )
                    },
                )
                return
            if metodo == "GET" and caminho == "/api/execucoes":
                self._json(200, {"execucoes": painel.store.listar_execucoes()})
                return
            if caminho.startswith("/api/execucoes/"):
                resto = caminho.removeprefix("/api/execucoes/")
                if metodo == "POST" and resto == "":
                    self._json(202, painel.iniciar(self._corpo()))
                    return
                if "/" in resto:
                    execucao_id, acao = resto.split("/", 1)
                else:
                    execucao_id, acao = resto, ""
                if not _ID.fullmatch(execucao_id):
                    raise ErroPainel(404, "Execução não encontrada.")
                if metodo == "GET" and acao == "":
                    dados = painel.store.obter_execucao(execucao_id)
                    if dados is None:
                        raise ErroPainel(404, "Execução não encontrada.")
                    self._json(200, dados)
                    return
                if metodo == "POST" and acao == "cancelar":
                    self._json(200, painel.cancelar(execucao_id))
                    return
            if metodo == "POST" and caminho == "/api/execucoes":
                self._json(202, painel.iniciar(self._corpo()))
                return
            if metodo == "GET" and caminho == "/api/arquivos":
                self._json(200, {"arquivos": painel.arquivos()})
                return
            if metodo == "GET" and caminho == "/api/arquivos/baixar":
                self._baixar(_um(consulta, "nome") or "")
                return
            if metodo == "POST" and caminho == "/api/pasta":
                abrir_pasta_saida()
                self._json(200, {"ok": True})
                return
            if metodo == "POST" and caminho == "/api/reset":
                self._json(200, painel.resetar(self._corpo()))
                return
            raise ErroPainel(404, "Rota não encontrada.")

        def _html(self) -> None:
            corpo = PAGINA.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

        def _baixar(self, nome: str) -> None:
            arquivo = painel.arquivo_autorizado(nome)
            corpo = arquivo.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header(
                "Content-Disposition", f'attachment; filename="{arquivo.name}"'
            )
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def _corpo(self) -> dict:
            tamanho = int(self.headers.get("Content-Length") or 0)
            if tamanho > 32_000:
                raise ErroPainel(400, "Pedido grande demais.")
            bruto = self.rfile.read(tamanho) if tamanho else b""
            if not bruto:
                return {}
            try:
                dados = json.loads(bruto.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ErroPainel(400, "JSON inválido.") from exc
            if not isinstance(dados, dict):
                raise ErroPainel(400, "Pedido inválido.")
            return dados

        def _json(self, codigo: int, dados: dict) -> None:
            corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

    return _ServidorLocal(("127.0.0.1", porta), Handler)


def _um(consulta: dict, chave: str) -> str | None:
    valores = consulta.get(chave) or []
    if not valores or not valores[0]:
        return None
    return valores[0]


def servir(porta: int = 8765, abrir: bool = False) -> None:
    if not isinstance(porta, int) or not 1 <= porta <= 65535:
        raise SystemExit(f"Porta inválida: {porta}")
    ensure_dirs()
    painel = Painel(Store(STATE / "dashboard.sqlite"))
    try:
        httpd = criar_servidor(painel, porta)
    except OSError as exc:
        raise SystemExit(
            f"Não foi possível escutar em 127.0.0.1:{porta}. "
            f"A porta pode estar ocupada. ({exc})"
        ) from exc
    url = f"http://127.0.0.1:{porta}/"
    print(f"Wago Pipeline disponível em {url}", flush=True)
    print(
        "O painel escuta só nesta máquina. Ctrl+C encerra o painel; "
        "execuções em andamento continuam no worker.",
        flush=True,
    )
    painel.painel()
    if abrir:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando o painel...")
    finally:
        httpd.server_close()
