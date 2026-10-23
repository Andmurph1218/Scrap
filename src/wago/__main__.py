"""CLI: python -m wago <comando>"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

# Garante que `src/` está no path quando executado como script
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wago.config import OUT, load_env  # noqa: E402


def _latest_master(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise SystemExit(f"Arquivo não encontrado: {path}")
        return path
    candidates = sorted(
        glob.glob(str(OUT / "* master.csv")),
        key=lambda p: Path(p).stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit(
            "Nenhum master.csv em data/out/. Rode `python -m wago scrape` antes."
        )
    return Path(candidates[0])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wago",
        description=(
            "Pipeline Wago → master → Nuvemshop (template) → imagens → "
            "Anunciar-ML.xlsx"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser(
        "discover", help="1) Baixa sitemap e grava links de produtos"
    )
    p_discover.add_argument(
        "--refresh",
        action="store_true",
        help="Força redownload mesmo se o JSON já existir",
    )

    p_scrape = sub.add_parser(
        "scrape", help="2) Scrape autenticado (preço + IPI + specs + fotos URL)"
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
        "export", help="3) Reexporta CSVs auxiliares a partir de um master.csv"
    )
    p_export.add_argument("master_csv", nargs="?", default=None)
    p_export.add_argument("--lote", type=int, default=1)

    p_ns = sub.add_parser(
        "fill-nuvemshop",
        help="4a) Preenche o CSV oficial de importação Nuvemshop",
    )
    p_ns.add_argument(
        "master_csv",
        nargs="?",
        default=None,
        help="Master scrape (default: latest em data/out/)",
    )
    p_ns.add_argument("-o", "--output", default=None)

    p_img = sub.add_parser(
        "images",
        help="4b) Baixa imagens em pastas SKU__nome",
    )
    p_img.add_argument("master_csv", nargs="?", default=None)
    p_img.add_argument(
        "--dir",
        default=None,
        help="Diretório base (default: data/out/imagens)",
    )
    p_img.add_argument("--limit", type=int, default=None)

    p_ml = sub.add_parser(
        "fill-ml",
        help="5) Preenche Anunciar-ML.xlsx (aba por categoria)",
    )
    p_ml.add_argument("master_csv", nargs="?", default=None)
    p_ml.add_argument(
        "--sheet",
        default="Outros",
        help="Aba do template (default: Outros)",
    )
    p_ml.add_argument("-o", "--output", default=None)

    # Opcional / legado
    p_photos = sub.add_parser(
        "photos",
        help="(Opcional) Upload de fotos via API Nuvemshop após import",
    )
    p_photos.add_argument("csv", nargs="?", default=None)

    p_ml_map = sub.add_parser(
        "ml-map", help="(Opcional) Gera mapa_geral_categorias_ml.json"
    )
    p_ml_map.add_argument("--max-nodes", type=int, default=None)
    p_ml_map.add_argument("--no-resume", action="store_true")

    p_ml_tpl = sub.add_parser(
        "ml-templates",
        help="(Opcional) Gera xlsx de categorias via API ML",
    )
    p_ml_tpl.add_argument("--limit", type=int, default=None)

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

        exportar_de_master(_latest_master(args.master_csv), lote_numero=args.lote)

    elif args.command == "fill-nuvemshop":
        from wago.fill_nuvemshop import fill_nuvemshop

        fill_nuvemshop(_latest_master(args.master_csv), output=args.output)

    elif args.command == "images":
        from wago.images import baixar_imagens_lote

        baixar_imagens_lote(
            _latest_master(args.master_csv),
            output_dir=args.dir,
            limit=args.limit,
        )

    elif args.command == "fill-ml":
        from wago.fill_ml import fill_ml

        fill_ml(
            _latest_master(args.master_csv),
            sheet=args.sheet,
            output=args.output,
        )

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
