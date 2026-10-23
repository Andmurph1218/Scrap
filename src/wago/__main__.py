"""CLI: python -m wago <comando>"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Garante que `src/` está no path quando executado como script
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wago.config import load_env  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wago",
        description="Pipeline Wago → Nuvemshop / Bling / Mercado Livre",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser(
        "discover", help="Baixa sitemap e grava links de produtos"
    )
    p_discover.add_argument(
        "--refresh",
        action="store_true",
        help="Força redownload mesmo se o JSON já existir",
    )

    p_scrape = sub.add_parser(
        "scrape", help="Scrape autenticado de produtos (preço + IPI + specs)"
    )
    p_scrape.add_argument("--limit", type=int, default=1000)
    p_scrape.add_argument("--lote", type=int, default=1)
    p_scrape.add_argument(
        "--refresh-links",
        action="store_true",
        help="Atualiza o sitemap antes de scrapear",
    )
    p_scrape.add_argument(
        "--headed",
        action="store_true",
        help="Abre o browser visível (debug)",
    )

    p_export = sub.add_parser(
        "export", help="Reexporta CSVs a partir de um master.csv"
    )
    p_export.add_argument("master_csv", help="Caminho do CSV master")
    p_export.add_argument("--lote", type=int, default=1)

    p_photos = sub.add_parser(
        "photos", help="Vincula fotos do lote aos produtos na Nuvemshop"
    )
    p_photos.add_argument(
        "csv",
        nargs="?",
        default=None,
        help="CSV com colunas SKU e Fotos (default: latest)",
    )

    p_ml_map = sub.add_parser(
        "ml-map", help="Etapa 1: gera mapa_geral_categorias_ml.json"
    )
    p_ml_map.add_argument(
        "--max-nodes",
        type=int,
        default=None,
        help="Limite de categorias (smoke test)",
    )
    p_ml_map.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignora mapa existente e recomeça",
    )

    p_ml_tpl = sub.add_parser(
        "ml-templates",
        help="Gera planilhas .xlsx das categorias folha alvo",
    )
    p_ml_tpl.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Máximo de planilhas novas nesta execução",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "discover":
        from wago.discover import carregar_links

        carregar_links(force_refresh=args.refresh or True)

    elif args.command == "scrape":
        from wago.scrape import rodar_scraper

        rodar_scraper(
            limite_lote=args.limit,
            lote_numero=args.lote,
            force_refresh_links=args.refresh_links,
            headless=not args.headed,
        )

    elif args.command == "export":
        from wago.export import exportar_de_master

        exportar_de_master(args.master_csv, lote_numero=args.lote)

    elif args.command == "photos":
        from wago.photos import vincular_fotos_lote

        vincular_fotos_lote(args.csv)

    elif args.command == "ml-map":
        from wago.ml.map_categories import gerar_mapa_geral

        gerar_mapa_geral(
            max_nodes=args.max_nodes,
            resume=not args.no_resume,
        )

    elif args.command == "ml-templates":
        from wago.ml.templates import gerar_planilhas_alvo

        gerar_planilhas_alvo(limit=args.limit)

    else:
        parser.error(f"Comando desconhecido: {args.command}")


if __name__ == "__main__":
    main()
