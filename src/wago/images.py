"""Etapa 4b: baixa imagens do scrape em pastas Nome+SKU."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from wago.config import OUT, ensure_dirs
from wago.extract import filtrar_melhores_fotos

IMAGENS_DIR = OUT / "imagens"


def _slug(texto: str, max_len: int = 60) -> str:
    texto = (
        unicodedata.normalize("NFD", str(texto))
        .encode("ascii", "ignore")
        .decode("utf-8")
    )
    texto = re.sub(r"[^\w\s-]", "", texto).strip().lower()
    texto = re.sub(r"[-\s]+", "-", texto).strip("-")
    return (texto or "produto")[:max_len]


def pasta_produto(sku: str, nome: str) -> str:
    """Nome da pasta: SKU__slug-do-nome. Sem caminho relativo nem separadores."""
    sku_clean = re.sub(r"[^\w-]+", "_", str(sku).strip()).strip("._") or "SEM-SKU"
    return f"{sku_clean}__{_slug(nome)}"


def diretorio_produto(base: Path, sku: str, nome: str) -> Path:
    """Pasta do SKU dentro de ``base``. Recusa caminho que escape do diretório."""
    base_resolvida = base.resolve()
    nome_pasta = pasta_produto(sku, nome)
    if nome_pasta in {"", ".", ".."} or "/" in nome_pasta or "\\" in nome_pasta:
        raise ValueError(f"Nome de pasta inválido para SKU {sku!r}")
    destino = (base_resolvida / nome_pasta).resolve()
    if destino != base_resolvida and base_resolvida not in destino.parents:
        raise ValueError(f"Pasta fora do diretório autorizado: {destino}")
    return destino


def _urls_fotos(raw: str) -> list[str]:
    if not raw or str(raw).lower() in ("nan", "none"):
        return []
    return filtrar_melhores_fotos(str(raw))


EXTENSOES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
TRANSITORIOS = {429, 500, 502, 503, 504}
MIN_BYTES = 16


def extensao_da_imagem(dados: bytes) -> str | None:
    """Extensão pelo conteúdo. A extensão da URL não decide o formato."""
    if len(dados) < MIN_BYTES:
        return None
    if dados.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if dados.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if dados.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(dados) >= 12 and dados.startswith(b"RIFF") and dados[8:12] == b"WEBP":
        return ".webp"
    return None


def _arquivo_valido(pasta: Path, indice: int) -> Path | None:
    for ext in EXTENSOES:
        candidato = pasta / f"{indice:02d}{ext}"
        if not candidato.is_file():
            continue
        try:
            if extensao_da_imagem(candidato.read_bytes()):
                return candidato
        except OSError:
            continue
    return None


def baixar_imagem(
    url: str,
    pasta: Path,
    indice: int,
    *,
    timeout: int = 30,
    tentativas: int = 3,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Grava ``NN.ext`` só depois de validar o conteúdo.

    Arquivo já válido no mesmo índice é ignorado. Falha transitória tenta de novo.
    """
    existente = _arquivo_valido(pasta, indice)
    if existente is not None:
        return {"status": "ignorada", "caminho": str(existente), "motivo": ""}

    cliente = session or requests
    ultimo = "falha desconhecida"
    for tentativa in range(1, max(tentativas, 1) + 1):
        try:
            res = cliente.get(url, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            ultimo = f"rede: {exc}"
            if tentativa < tentativas:
                continue
            return {"status": "falha", "caminho": "", "motivo": ultimo}
        except requests.RequestException as exc:
            return {"status": "falha", "caminho": "", "motivo": str(exc)}

        if res.status_code in TRANSITORIOS and tentativa < tentativas:
            ultimo = f"HTTP {res.status_code}"
            continue
        if res.status_code != 200:
            return {
                "status": "falha",
                "caminho": "",
                "motivo": f"HTTP {res.status_code}",
            }
        dados = res.content or b""
        ext = extensao_da_imagem(dados)
        if ext is None:
            return {
                "status": "falha",
                "caminho": "",
                "motivo": "conteúdo não é uma imagem reconhecida",
            }
        pasta.mkdir(parents=True, exist_ok=True)
        final = pasta / f"{indice:02d}{ext}"
        temporario = pasta / f".{indice:02d}.part"
        try:
            temporario.write_bytes(dados)
            if extensao_da_imagem(temporario.read_bytes()) != ext:
                temporario.unlink(missing_ok=True)
                return {
                    "status": "falha",
                    "caminho": "",
                    "motivo": "arquivo gravado não confere",
                }
            temporario.replace(final)
        except OSError as exc:
            temporario.unlink(missing_ok=True)
            return {"status": "falha", "caminho": "", "motivo": str(exc)}
        return {"status": "baixada", "caminho": str(final), "motivo": ""}
    return {"status": "falha", "caminho": "", "motivo": ultimo}


def _publicar_resumo(base: Path, resumo: dict[str, Any]) -> None:
    path = base / "resumo-imagens.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def baixar_imagens_lote(
    master_csv: Path | str,
    *,
    output_dir: Path | str | None = None,
    limit: int | None = None,
    timeout: int = 30,
    tentativas: int = 3,
    dry_run: bool = False,
    session: requests.Session | None = None,
    cancelado=None,
) -> Path:
    """
    Para cada produto do master, cria pasta SKU__nome e baixa as fotos.
    """
    ensure_dirs()
    path = Path(master_csv)
    if not path.exists():
        raise SystemExit(f"Master não encontrado: {path}")

    base = Path(output_dir) if output_dir else IMAGENS_DIR
    if not dry_run:
        base.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    produtos: list[dict[str, Any]] = []
    baixadas = ignoradas = falhas = 0
    cliente = session or requests.Session()

    interromper = False
    for idx, row in enumerate(df.to_dict(orient="records")):
        if cancelado and cancelado():
            print("Cancelamento pedido. Imagens já validadas foram mantidas.")
            break
        if limit is not None and idx >= limit:
            break
        sku = str(row.get("SKU", "")).strip()
        nome = str(row.get("Nome", "")).strip() or sku
        if not sku:
            falhas += 1
            produtos.append(
                {
                    "sku": "",
                    "baixadas": 0,
                    "ignoradas": 0,
                    "falhas": 1,
                    "erros": ["SKU ausente"],
                }
            )
            continue
        try:
            pasta = diretorio_produto(base, sku, nome)
        except ValueError as exc:
            falhas += 1
            produtos.append(
                {
                    "sku": sku,
                    "baixadas": 0,
                    "ignoradas": 0,
                    "falhas": 1,
                    "erros": [str(exc)],
                }
            )
            continue

        urls = _urls_fotos(row.get("Fotos", ""))
        if not urls:
            falhas += 1
            produtos.append(
                {
                    "sku": sku,
                    "baixadas": 0,
                    "ignoradas": 0,
                    "falhas": 1,
                    "erros": ["sem URLs de foto"],
                }
            )
            print(f"  {sku}: sem URLs de foto")
            continue

        contagem = {"baixadas": 0, "ignoradas": 0, "falhas": 0}
        erros: list[str] = []
        for i, url in enumerate(urls, start=1):
            if cancelado and cancelado():
                print("Cancelamento pedido. Imagens já validadas foram mantidas.")
                interromper = True
                break
            if dry_run:
                print(f"  {sku}: simularia {i:02d} {url}")
                continue
            resultado = baixar_imagem(
                url,
                pasta,
                i,
                timeout=timeout,
                tentativas=tentativas,
                session=cliente,
            )
            status = str(resultado["status"])
            if status == "baixada":
                contagem["baixadas"] += 1
                baixadas += 1
            elif status == "ignorada":
                contagem["ignoradas"] += 1
                ignoradas += 1
            else:
                contagem["falhas"] += 1
                falhas += 1
                erros.append(f"{url}: {resultado['motivo']}")
                print(f"  Falha {sku} {url}: {resultado['motivo']}")

        if not dry_run:
            print(
                f"  {pasta.name}: baixadas={contagem['baixadas']} "
                f"ignoradas={contagem['ignoradas']} falhas={contagem['falhas']}"
            )
        produtos.append({"sku": sku, **contagem, "erros": erros})
        if interromper:
            break

    resumo = {
        "origem": str(path),
        "diretorio": str(base),
        "baixadas": baixadas,
        "ignoradas": ignoradas,
        "falhas": falhas,
        "produtos": produtos,
    }
    print(
        f"Imagens: baixadas={baixadas} ignoradas={ignoradas} falhas={falhas}"
    )
    if dry_run:
        print("Simulação: nenhum arquivo foi gravado.")
        return base
    _publicar_resumo(base, resumo)
    print(f"Diretório: {base}")
    return base
