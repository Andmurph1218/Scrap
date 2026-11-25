"""Orquestrador do pipeline Gavazzi.

Lê a planilha de preços → resolve URLs → scrape → traduz → exporta
Nuvemshop + Mercado Livre. Registra falhas e permite retomada a partir
do último `gavazzi-master-*.json` gerado.
"""

from __future__ import annotations

import glob
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from wago.config import OUT, STATE
from wago.gavazzi import discover, exporter_ml, exporter_nuvemshop, reader, scraper, translator

FALHAS_JSON = STATE / "gavazzi_falhas.json"


def _registrar_falha(codigo: str, motivo: str) -> None:
    falhas: dict[str, str] = {}
    if FALHAS_JSON.exists():
        try:
            falhas = json.loads(FALHAS_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            falhas = {}
    falhas[codigo] = motivo
    FALHAS_JSON.parent.mkdir(parents=True, exist_ok=True)
    FALHAS_JSON.write_text(
        json.dumps(falhas, ensure_ascii=False, indent=1), encoding="utf-8")


def _master_anterior(saida: Path) -> dict[str, dict]:
    """Produtos já processados no master mais recente (retomada)."""
    candidatos = sorted(
        glob.glob(str(saida / "gavazzi-master_*.json")),
        key=lambda p: Path(p).stat().st_mtime,
        reverse=True)
    if not candidatos:
        return {}
    try:
        lista = json.loads(Path(candidatos[0]).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {p["codigo"]: p for p in lista if p.get("codigo")}


def rodar(planilha: str | Path, saida: str | Path | None = None, *,
          limit: int | None = None, sem_ia: bool = False,
          refresh_links: bool = False, dry_run: bool = False,
          coluna_codigo: str | None = None,
          coluna_preco: str | None = None,
          cancelado: Any | None = None) -> dict[str, Any]:
    saida = Path(saida) if saida else OUT
    itens = reader.ler_planilha(planilha, coluna_codigo=coluna_codigo,
                                coluna_preco=coluna_preco)
    if limit:
        itens = itens[:limit]
    print(f"Planilha: {len(itens)} produto(s)")

    mapa = discover.carregar_mapa(force_refresh=refresh_links)
    codigos_planilha = {item["codigo"] for item in itens}
    feitos = {
        codigo: prod for codigo, prod in _master_anterior(saida).items()
        if codigo in codigos_planilha
    }

    sessao = scraper.nova_sessao()
    processados: list[dict] = list(feitos.values())
    falhas = 0

    for i, item in enumerate(itens, 1):
        if cancelado and cancelado():
            from wago.dashboard.service import Cancelado

            print("Cancelamento pedido: parando antes do próximo produto.")
            raise Cancelado()
        codigo = item["codigo"]
        if codigo in feitos:
            # retomada: preserva o preço mais recente da planilha
            if feitos[codigo].get("preco") != item["preco"]:
                feitos[codigo]["preco"] = item["preco"]
            continue
        url = discover.resolver_url(codigo, mapa)
        if not url:
            _registrar_falha(codigo, "produto não encontrado no site")
            print(f"  [{i}/{len(itens)}] {codigo}: não encontrado no site")
            falhas += 1
            continue
        try:
            produto = scraper.scrapear_produto(url, codigo=codigo, session=sessao)
            produto["preco"] = item["preco"]
            print(f"  [{i}/{len(itens)}] {codigo}: traduzindo…")
            traduzido = translator.traduzir(produto, sem_ia=sem_ia)
            # título vazio ou igual ao código → monta a partir da 1ª frase da descrição
            if not traduzido.get("titulo_pt") or traduzido["titulo_pt"].upper() == codigo:
                base = str(traduzido.get("descricao_pt") or traduzido.get("descricao") or codigo)
                primeira = base.split(",")[0].split(".")[0].strip()
                primeira = primeira.removesuffix(codigo).strip(" -–—")
                traduzido["titulo_pt"] = f"{primeira} {codigo}".strip()[:120]
            processados.append(traduzido)
            feitos[codigo] = traduzido
            print(f"  [{i}/{len(itens)}] {codigo}: ok")
        except Exception as exc:
            _registrar_falha(codigo, str(exc)[:200])
            print(f"  [{i}/{len(itens)}] {codigo}: falha — {exc}")
            falhas += 1

    if dry_run:
        print(f"Dry-run: {len(processados)} processado(s), {falhas} falha(s)")
        return {"processados": len(processados), "falhas": falhas,
                "master": None, "nuvemshop": None, "ml": None}

    saida.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%d-%m_%H%M%S")
    master_path = saida / f"gavazzi-master_{stamp}.json"
    master_path.write_text(
        json.dumps(processados, ensure_ascii=False, indent=1), encoding="utf-8")

    if not processados:
        print("Nenhum produto processado — planilhas não geradas. "
              "Verifique data/state/gavazzi_falhas.json")
        return {"processados": 0, "falhas": falhas,
                "master": master_path, "nuvemshop": None, "ml": None}

    ns = exporter_nuvemshop.exportar(processados, saida=saida)
    ml = exporter_ml.exportar(processados, saida=saida)
    print(f"Nuvemshop: {ns['csv']}")
    print(f"Imagens:   {ns['imagens']}")
    print(f"ML:        {ml['xlsx']}")

    return {"processados": len(processados), "falhas": falhas,
            "master": master_path, "nuvemshop": ns, "ml": ml}
