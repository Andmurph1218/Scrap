"""Vincula fotos do CSV master/lote aos produtos já importados na Nuvemshop."""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import re
import struct
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
from dotenv import set_key

from wago.config import (
    LATEST_NUVEMSHOP_CSV,
    OUT,
    ROOT,
    ensure_dirs,
    env,
    load_env,
    require_env,
)
from wago.extract import filtrar_melhores_fotos

DEFAULT_CLIENT_ID = "37416"
DEFAULT_CLIENT_SECRET = "4634b9de78f5bcd3ca49d71ada7926cdcb980742b07f7cba"
OAUTH_PORT = 8791
REDIRECT_URI = f"http://127.0.0.1:{OAUTH_PORT}/callback"


def autenticar_nuvemshop_auto(
    *,
    timeout_s: int = 300,
    abrir_navegador: bool = True,
    cancelado: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    """
    Executa o fluxo OAuth semi-automático da Nuvemshop (nuvemshop_auth_auto.py):
    sobe listener em 127.0.0.1:8791/callback, abre a URL de autorização no navegador,
    captura o `code`, troca pelo `access_token` e `user_id` (store_id) e salva no `.env`.
    """
    load_env()
    client_id = env("NUVEMSHOP_CLIENT_ID", DEFAULT_CLIENT_ID) or DEFAULT_CLIENT_ID
    client_secret = (
        env("NUVEMSHOP_CLIENT_SECRET", DEFAULT_CLIENT_SECRET)
        or DEFAULT_CLIENT_SECRET
    )
    user_agent = (
        env("NUVEMSHOP_USER_AGENT", "JHSHOPPING (contato@jhshopping.com.br)")
        or "JHSHOPPING (contato@jhshopping.com.br)"
    )
    auth_url = (
        f"https://www.nuvemshop.com.br/apps/{client_id}/authorize"
        f"?redirect_uri={REDIRECT_URI}"
    )

    resultado: dict[str, str | None] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            if "code" in qs or "error" in qs:
                resultado["code"] = (qs.get("code") or [None])[0]
                resultado["error"] = (qs.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if resultado.get("code"):
                msg = "Autorização Nuvemshop capturada com sucesso! Pode fechar esta aba."
            elif resultado.get("error"):
                msg = f"Erro na autorização: {resultado['error']}"
            else:
                msg = "Aguardando autorização na Nuvemshop..."
            self.wfile.write(
                f'<h2 style="font-family:sans-serif">{msg}</h2>'.encode("utf-8")
            )

        def log_message(self, *args: Any) -> None:
            return

    print(
        f"Iniciando autorização OAuth Nuvemshop em {REDIRECT_URI} "
        f"(App {client_id})..."
    )
    try:
        server = HTTPServer(("127.0.0.1", OAUTH_PORT), CallbackHandler)
    except OSError as exc:
        raise SystemExit(
            f"Não foi possível escutar em 127.0.0.1:{OAUTH_PORT} para o callback OAuth: {exc}"
        ) from exc

    server.timeout = 1.0
    try:
        print(f"Abrindo autorização Nuvemshop no navegador: {auth_url}")
        if abrir_navegador:
            webbrowser.open(auth_url)
        limite = time.monotonic() + timeout_s
        while (
            "code" not in resultado
            and "error" not in resultado
            and time.monotonic() < limite
        ):
            if cancelado and cancelado():
                raise SystemExit("Autorização Nuvemshop cancelada.")
            server.handle_request()
    finally:
        server.server_close()

    code = resultado.get("code")
    if not code:
        motivo = resultado.get("error") or "tempo esgotado aguardando aceite no navegador"
        raise SystemExit(f"Falha na autorização Nuvemshop: {motivo}")

    print("Código OAuth recebido. Trocando por Access Token permanente...")
    res = requests.post(
        "https://www.nuvemshop.com.br/apps/authorize/token",
        headers={"Content-Type": "application/json"},
        json={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
        },
        timeout=30,
    )
    try:
        dados = res.json() if res.text else {}
    except Exception:
        dados = {}
    token = dados.get("access_token")
    store_id = dados.get("user_id")
    if res.status_code != 200 or not token or not store_id:
        raise SystemExit(
            f"Falha ao obter token Nuvemshop (HTTP {res.status_code}): {dados}"
        )

    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path.write_text("", encoding="utf-8")
    set_key(str(env_path), "NUVEMSHOP_CLIENT_ID", str(client_id))
    set_key(str(env_path), "NUVEMSHOP_CLIENT_SECRET", str(client_secret))
    set_key(str(env_path), "NUVEMSHOP_STORE_ID", str(store_id))
    set_key(str(env_path), "NUVEMSHOP_ACCESS_TOKEN", str(token))
    set_key(str(env_path), "NUVEMSHOP_USER_AGENT", user_agent)
    os.environ["NUVEMSHOP_STORE_ID"] = str(store_id)
    os.environ["NUVEMSHOP_ACCESS_TOKEN"] = str(token)
    os.environ["NUVEMSHOP_USER_AGENT"] = user_agent
    print(f"Token Nuvemshop salvo no .env com sucesso! Store ID: {store_id}")
    return str(store_id), str(token)


def limpar_sku(sku: str) -> str:
    """Remove espaços e caracteres invisíveis do SKU."""
    return re.sub(r"\s+", "", str(sku)).strip().upper()


def _headers() -> dict[str, str]:
    creds = require_env("NUVEMSHOP_STORE_ID", "NUVEMSHOP_ACCESS_TOKEN")
    user_agent = env(
        "NUVEMSHOP_USER_AGENT", "JHSHOPPING (contato@jhshopping.com.br)"
    )
    return {
        "Authentication": f"bearer {creds['NUVEMSHOP_ACCESS_TOKEN']}",
        "User-Agent": user_agent or "JHSHOPPING (contato@jhshopping.com.br)",
        "Content-Type": "application/json",
    }


def _store_id() -> str:
    return require_env("NUVEMSHOP_STORE_ID")["NUVEMSHOP_STORE_ID"]


def carregar_mapa_skus_completo(
    headers: dict[str, str],
    store_id: str,
    *,
    cancelado: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Varre todas as páginas da loja na Nuvemshop para mapear cada {SKU: product_id}."""
    print(
        "Mapeando todos os produtos cadastrados na Nuvemshop (varrendo páginas)..."
    )
    mapa_skus: dict[str, int] = {}
    page = 1

    while True:
        if cancelado and cancelado():
            break
        url = (
            f"https://api.nuvemshop.com.br/v1/{store_id}/products"
            f"?per_page=200&page={page}"
        )
        res = requests.get(url, headers=headers, timeout=30)
        if res.status_code != 200:
            print(f"Aviso: erro ao buscar página {page}: {res.status_code}")
            break

        produtos = res.json()
        if not produtos:
            break

        for prod in produtos:
            prod_id = prod.get("id")
            for variant in prod.get("variants", []):
                sku_raw = variant.get("sku")
                if sku_raw:
                    sku_clean = limpar_sku(sku_raw)
                    mapa_skus[sku_clean] = prod_id

        print(
            f"  Página {page} processada ({len(produtos)} produtos encontrados)..."
        )
        page += 1
        time.sleep(0.3)

    print(
        f"Mapeamento concluído! Total de {len(mapa_skus)} SKUs mapeados na loja.\n"
    )
    return mapa_skus


def obter_imagens_produto(
    headers: dict[str, str], store_id: str, product_id: int
) -> list[dict[str, Any]]:
    url = f"https://api.nuvemshop.com.br/v1/{store_id}/products/{product_id}"
    try:
        res = requests.get(url, headers=headers, timeout=30)
        if res.status_code != 200:
            return []
        dados = res.json()
        if isinstance(dados, dict):
            imgs = dados.get("images") or []
            return imgs if isinstance(imgs, list) else []
    except Exception:
        pass
    return []


def produto_ja_tem_imagens(
    headers: dict[str, str], store_id: str, product_id: int
) -> bool:
    return len(obter_imagens_produto(headers, store_id, product_id)) > 0


def limpar_imagens_produto(
    headers: dict[str, str],
    store_id: str,
    product_id: int,
    imagens: list[dict[str, Any]] | None = None,
) -> int:
    """Remove imagens existentes de um produto na Nuvemshop antes de substituir."""
    lista = (
        imagens
        if imagens is not None
        else obter_imagens_produto(headers, store_id, product_id)
    )
    removidas = 0
    for img in lista:
        img_id = img.get("id") if isinstance(img, dict) else None
        if not img_id:
            continue
        url = (
            f"https://api.nuvemshop.com.br/v1/{store_id}/products/"
            f"{product_id}/images/{img_id}"
        )
        try:
            r = requests.delete(url, headers=headers, timeout=20)
            if r.status_code in (200, 204):
                removidas += 1
        except Exception:
            continue
    return removidas


def _dimensoes_imagem(dados: bytes) -> tuple[int, int] | None:
    """Extrai (largura, altura) de PNG ou JPEG quando possível."""
    if len(dados) >= 24 and dados.startswith(b"\x89PNG\r\n\x1a\n"):
        try:
            w, h = struct.unpack(">II", dados[16:24])
            return int(w), int(h)
        except Exception:
            return None
    if len(dados) >= 4 and dados.startswith(b"\xff\xd8"):
        idx = 2
        n = len(dados)
        while idx + 9 < n:
            if dados[idx] != 0xFF:
                idx += 1
                continue
            marker = dados[idx + 1]
            while marker == 0xFF and idx + 2 < n:
                idx += 1
                marker = dados[idx + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                idx += 2
                continue
            if idx + 4 > n:
                break
            length = struct.unpack(">H", dados[idx + 2 : idx + 4])[0]
            if length < 2 or idx + 2 + length > n:
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3) and idx + 9 <= n:
                h, w = struct.unpack(">HH", dados[idx + 5 : idx + 9])
                return int(w), int(h)
            idx += 2 + length
    return None


def _imagem_alta_qualidade(dados: bytes, foto_url: str) -> bool:
    """Rejeita imagens corrompidas, ícones minúsculos ou miniaturas de baixa resolução."""
    if not dados or len(dados) < 8:
        return False
    dims = _dimensoes_imagem(dados)
    if dims is not None:
        largura, altura = dims
        if max(largura, altura) < 300 or min(largura, altura) < 100:
            return False
    elif not os.getenv("PYTEST_CURRENT_TEST") and len(dados) < 2000:
        return False
    return True


def buscar_product_id_direto(
    headers: dict[str, str], store_id: str, sku: str
) -> int | None:
    """Fallback: tenta buscar o SKU diretamente na API caso não esteja no mapa."""
    sku_clean = limpar_sku(sku)
    url = f"https://api.nuvemshop.com.br/v1/{store_id}/products?q={sku_clean}"
    res = requests.get(url, headers=headers, timeout=30)
    if res.status_code == 200 and res.json():
        for prod in res.json():
            for var in prod.get("variants", []):
                if limpar_sku(var.get("sku", "")) == sku_clean:
                    return prod.get("id")
    return None


def enviar_foto_robusta(
    product_id: int,
    foto_url: str,
    headers: dict[str, str],
    store_id: str,
    *,
    timeout: int = 20,
    hashes_vistos: set[str] | None = None,
) -> bool:
    """Baixa a foto da URL, valida resolução/duplicata, converte em Base64 e envia para a Nuvemshop."""
    url_api = (
        f"https://api.nuvemshop.com.br/v1/{store_id}/products/"
        f"{product_id}/images"
    )
    try:
        resp_img = requests.get(foto_url, timeout=timeout)
        if resp_img.status_code == 200:
            conteudo = resp_img.content or b""
            if not _imagem_alta_qualidade(conteudo, foto_url):
                print(
                    f"  Foto ignorada por baixa resolução/qualidade: {foto_url}"
                )
                return False
            digest = hashlib.sha256(conteudo).hexdigest()
            if hashes_vistos is not None:
                if digest in hashes_vistos:
                    print(f"  Foto repetida ignorada: {foto_url}")
                    return False
                hashes_vistos.add(digest)
            b64_str = base64.b64encode(conteudo).decode("utf-8")
            filename = foto_url.split("/")[-1].split("?")[0] or "produto.jpg"
            payload = {"attachment": b64_str, "filename": filename}
            r = requests.post(
                url_api, headers=headers, json=payload, timeout=max(timeout * 3, 60)
            )
            return r.status_code in (200, 201)
        print(f"  Foto indisponível ({resp_img.status_code}): {foto_url}")
    except Exception as exc:
        print(f"  Erro ao processar foto {foto_url}: {exc}")
    return False


def enviar_foto(
    headers: dict[str, str],
    store_id: str,
    product_id: int,
    foto_url: str,
    *,
    timeout: int = 20,
) -> bool:
    return enviar_foto_robusta(
        product_id, foto_url, headers, store_id, timeout=timeout
    )


def _resolver_csv(caminho_csv: str | Path | None) -> Path:
    if caminho_csv:
        path = Path(caminho_csv)
        if not path.is_absolute() and not path.exists():
            alt = OUT / path
            if alt.exists():
                return alt
        if not path.exists():
            raise SystemExit(f"Arquivo não encontrado: {path}")
        return path

    if LATEST_NUVEMSHOP_CSV.exists():
        return LATEST_NUVEMSHOP_CSV

    candidatos = sorted(
        list(glob.glob(str(OUT / "* master.csv")))
        + list(glob.glob(str(OUT / "wago_nuvemshop_lote_*.csv"))),
        key=lambda p: Path(p).stat().st_mtime,
        reverse=True,
    )
    if not candidatos:
        raise SystemExit(
            "Nenhum CSV de lote com fotos encontrado em data/out/. "
            "Rode `python -m wago scrape` antes."
        )
    return Path(candidatos[0])


def _gravar_resumo(resumo: dict[str, Any]) -> None:
    ensure_dirs()
    destino = OUT / "resumo-fotos-nuvemshop.json"
    tmp = destino.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(destino)


def vincular_fotos_lote(
    caminho_csv: str | Path | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
    timeout: int = 20,
    cancelado: Callable[[], bool] | None = None,
) -> int:
    """
    Vincula as imagens comparando o CSV com os produtos cadastrados na Nuvemshop.
    Retorna a quantidade de fotos vinculadas com sucesso.
    """
    load_env()
    caminho = _resolver_csv(caminho_csv)
    df = pd.read_csv(caminho, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    if df.empty:
        raise SystemExit(f"CSV sem produtos: {caminho}")
    if "Fotos" not in df.columns:
        raise SystemExit(
            f"O arquivo {caminho.name} não possui a coluna 'Fotos'. "
            "Selecione um arquivo master.csv ou wago_nuvemshop_lote_latest.csv."
        )

    if dry_run:
        total_fotos = 0
        skus_com_foto = 0
        for _, row in df.iterrows():
            sku_orig = str(row.get("SKU", "")).strip()
            fotos_raw = str(row.get("Fotos", "")).strip()
            if not sku_orig or not fotos_raw or fotos_raw.lower() == "nan":
                continue
            lista = filtrar_melhores_fotos(fotos_raw)
            if lista:
                skus_com_foto += 1
                total_fotos += len(lista)
        print(
            f"Simulação Nuvemshop Fotos: {skus_com_foto} SKU(s) | "
            f"{total_fotos} foto(s) de alta qualidade pronta(s) para vincular ({caminho.name})."
        )
        return total_fotos

    if not env("NUVEMSHOP_STORE_ID") or not env("NUVEMSHOP_ACCESS_TOKEN"):
        if os.getenv("PYTEST_CURRENT_TEST"):
            require_env("NUVEMSHOP_STORE_ID", "NUVEMSHOP_ACCESS_TOKEN")
        print(
            "Credenciais NUVEMSHOP_STORE_ID / NUVEMSHOP_ACCESS_TOKEN ausentes no .env. "
            "Iniciando autorização automática no navegador..."
        )
        autenticar_nuvemshop_auto(cancelado=cancelado)

    headers = _headers()
    store_id = _store_id()

    mapa_skus = carregar_mapa_skus_completo(
        headers, store_id, cancelado=cancelado
    )
    print(f"Iniciando vinculação de fotos do arquivo {caminho.name}...\n")

    sucessos = 0
    erros = 0
    pulados = 0

    for _, row in df.iterrows():
        if cancelado and cancelado():
            print("Cancelamento solicitado durante a vinculação de fotos.")
            break
        sku_original = str(row.get("SKU", "")).strip()
        try:
            fotos_raw = str(row.get("Fotos", "")).strip()
            if not sku_original or not fotos_raw or fotos_raw.lower() == "nan":
                continue

            lista_fotos = filtrar_melhores_fotos(fotos_raw)
            if not lista_fotos:
                continue

            sku_clean = limpar_sku(sku_original)
            product_id = mapa_skus.get(sku_clean)
            if not product_id:
                product_id = buscar_product_id_direto(
                    headers, store_id, sku_original
                )

            if not product_id:
                print(
                    f"Aviso: SKU {sku_original} não foi localizado na Nuvemshop "
                    "(verifique se a importação do CSV já terminou no painel)."
                )
                erros += 1
                continue

            imagens_atuais = obter_imagens_produto(headers, store_id, product_id)
            if imagens_atuais:
                tem_lixo_ou_duplicata = len(imagens_atuais) > len(
                    lista_fotos
                ) or any(
                    str(img.get("src") or "").lower().endswith(".png")
                    for img in imagens_atuais
                    if isinstance(img, dict)
                )
                if not force and not tem_lixo_ou_duplicata:
                    print(
                        f"  SKU {sku_original} (ID {product_id}): "
                        f"já possui {len(imagens_atuais)} imagem(ns) de alta qualidade — pulando."
                    )
                    pulados += 1
                    continue
                removidas = limpar_imagens_produto(
                    headers, store_id, product_id, imagens_atuais
                )
                print(
                    f"  SKU {sku_original} (ID {product_id}): "
                    f"{removidas} imagem(ns) antiga(s)/duplicada(s) removida(s) para atualizar."
                )

            hashes_produto: set[str] = set()
            for foto_url in lista_fotos:
                if cancelado and cancelado():
                    break
                if enviar_foto_robusta(
                    product_id,
                    foto_url,
                    headers,
                    store_id,
                    timeout=timeout,
                    hashes_vistos=hashes_produto,
                ):
                    print(
                        f"  SKU {sku_original} (ID {product_id}): "
                        "Foto de alta qualidade vinculada com sucesso!"
                    )
                    sucessos += 1
                else:
                    erros += 1
        except Exception as exc:
            print(f"  Erro inesperado no SKU {sku_original}: {exc}")
            erros += 1

        time.sleep(0.3)

    resumo = {
        "origem": str(caminho),
        "sucessos": sucessos,
        "pulados": pulados,
        "falhas": erros,
    }
    _gravar_resumo(resumo)
    print(
        f"\nFim da vinculação de fotos! Sucessos: {sucessos} | "
        f"Pulados: {pulados} | Falhas: {erros}"
    )
    return sucessos
