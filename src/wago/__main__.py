"""CLI: python -m wago <comando>"""

from __future__ import annotations

import argparse
import glob
import json
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
            "Pipeline Scraping Operation — Wago → master → Nuvemshop (template) → imagens → "
            "Anunciar-ML.xlsx + Motor de Categorias (taxonomia)"
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
    p_ns.add_argument(
        "--dry-run",
        action="store_true",
        help="Valida e mostra o resumo sem gravar",
    )
    p_ns.add_argument(
        "--force",
        action="store_true",
        help="Substitui o arquivo de saída se ele já existir",
    )

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
    p_img.add_argument("--timeout", type=int, default=30)
    p_img.add_argument("--tentativas", type=int, default=3)
    p_img.add_argument(
        "--dry-run",
        action="store_true",
        help="Lista as fotos sem baixar",
    )

    p_ml = sub.add_parser(
        "fill-ml",
        help="5) Preenche Anunciar-ML.xlsx (aba por categoria)",
    )
    p_ml.add_argument("master_csv", nargs="?", default=None)
    p_ml.add_argument(
        "--sheet",
        default="Automático",
        help="Aba do template ou 'Automático' por categoria do produto (default: Automático)",
    )
    p_ml.add_argument("-o", "--output", default=None)
    p_ml.add_argument(
        "--dry-run",
        action="store_true",
        help="Valida e mostra o resumo sem gravar",
    )
    p_ml.add_argument(
        "--force",
        action="store_true",
        help="Substitui o arquivo de saída se ele já existir",
    )

    # Upload de fotos na Nuvemshop via API após import do CSV
    p_photos = sub.add_parser(
        "photos",
        help="4c) Vincula fotos via API Nuvemshop após importar o CSV na loja",
    )
    p_photos.add_argument("csv", nargs="?", default=None)
    p_photos.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula a leitura das fotos sem chamar a API da Nuvemshop",
    )
    p_photos.add_argument(
        "--force",
        action="store_true",
        help="Envia as fotos mesmo se o produto já tiver imagens na loja",
    )
    p_photos.add_argument("--timeout", type=int, default=20)

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

    p_ns_auth = sub.add_parser(
        "nuvemshop-auth",
        help="Autoriza o app na Nuvemshop via navegador e grava token/store_id no .env",
    )
    p_ns_auth.add_argument("--timeout", type=int, default=300)

    p_tax = sub.add_parser(
        "taxonomia",
        help="Motor de Categorias (PIM) da Nuvemshop",
    )
    tax_sub = p_tax.add_subparsers(dest="acao", required=True)
    tax_sub.add_parser(
        "inventario",
        help="Snapshot somente leitura de categorias/produtos + diagnóstico",
    )
    tax_sub.add_parser(
        "diagnostico", help="Recalcula o diagnóstico do último snapshot (sem chamar a API)"
    )
    tax_sub.add_parser(
        "migrar-decisoes",
        help="Faz backup de decisoes.json e grava o caminho estável de cada folha",
    )
    p_tax_gerar = tax_sub.add_parser(
        "gerar", help="Gera a taxonomia-alvo proposta (pendente de aprovação)"
    )
    p_tax_gerar.add_argument("--snapshot", type=Path, default=None)
    tax_sub.add_parser("status", help="Mostra o status do gate de aprovação")
    p_tax_aprovar = tax_sub.add_parser("aprovar", help="Aprova a taxonomia proposta")
    p_tax_aprovar.add_argument("--por", required=True, help="Nome do operador")
    p_tax_class = tax_sub.add_parser(
        "classificar", help="Classifica os produtos na taxonomia aprovada (Fase 3)"
    )
    p_tax_class.add_argument("--sem-ia", action="store_true", help="Somente regras determinísticas")
    tax_sub.add_parser(
        "plano", help="Gera o plano de mudanças em modo dry-run (nada é enviado à Nuvemshop)"
    )
    p_tax_aplicar = tax_sub.add_parser("aplicar", help="Aplica o plano na Nuvemshop (escrita)")
    p_tax_aplicar.add_argument("--piloto", action="store_true", help="Somente o mini-lote de validação")
    p_tax_aplicar.add_argument("--retomar", type=Path, default=None,
                               help="Diário aplicacao-*.json de uma execução interrompida (pula o que já foi feito)")
    p_tax_rev = tax_sub.add_parser("reverter", help="Desfaz uma aplicação (diário aplicacao-*.json)")
    p_tax_rev.add_argument("aplicacao", type=Path)
    tax_sub.add_parser(
        "simular", help="Estima (sem IA) produtos por categoria da proposta pendente"
    )
    p_tax_lote = tax_sub.add_parser(
        "editar-lote", help="Aplica um arquivo JSON de edições na taxonomia (abre nova versão)"
    )
    p_tax_orfas = tax_sub.add_parser(
        "excluir-orfas", help="Exclui categorias fora da taxonomia cujos vínculos são só ancestrais órfãos"
    )
    p_tax_orfas.add_argument("categorias", type=int, nargs="+", help="IDs das categorias a excluir")
    p_tax_lote.add_argument("arquivo", type=Path)
    p_tax_editar = tax_sub.add_parser("editar", help="Edita a taxonomia proposta")
    p_tax_editar.add_argument(
        "operacao", choices=["renomear", "mover", "mesclar", "excluir", "adicionar", "sinonimos"]
    )
    p_tax_editar.add_argument("--sinonimos", nargs="*", default=None)
    p_tax_editar.add_argument("--id", dest="id_alvo")
    p_tax_editar.add_argument("--nome")
    p_tax_editar.add_argument("--pai")
    p_tax_editar.add_argument("--origem")
    p_tax_editar.add_argument("--destino")

    p_dash = sub.add_parser(
        "dashboard",
        help="Abre o painel local em 127.0.0.1",
    )
    p_worker = sub.add_parser(
        "worker",
        help="Consome a fila de execuções do painel (processo separado)",
    )
    p_worker.add_argument(
        "--ocioso",
        type=float,
        default=120.0,
        help="Segundos sem trabalho antes de encerrar (default: 120)",
    )

    sub.add_parser(
        "status", help="Contagem de SKUs por status no catálogo"
    )

    p_dash.add_argument("--port", type=int, default=8765)
    p_dash.add_argument(
        "--open",
        action="store_true",
        dest="abrir",
        help="Abre o navegador depois que o painel estiver no ar",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "discover":
        from wago.discover import carregar_links

        carregar_links(force_refresh=bool(args.refresh))

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

        fill_nuvemshop(
            _latest_master(args.master_csv),
            output=args.output,
            dry_run=args.dry_run,
            force=args.force,
        )
        if not args.dry_run:
            from wago.state import sincronizar_saidas

            sincronizar_saidas()

    elif args.command == "images":
        from wago.images import baixar_imagens_lote

        baixar_imagens_lote(
            _latest_master(args.master_csv),
            output_dir=args.dir,
            limit=args.limit,
            timeout=args.timeout,
            tentativas=args.tentativas,
            dry_run=args.dry_run,
        )
        if not args.dry_run and args.dir is None:
            from wago.state import sincronizar_saidas

            sincronizar_saidas()

    elif args.command == "fill-ml":
        from wago.fill_ml import fill_ml

        fill_ml(
            _latest_master(args.master_csv),
            sheet=args.sheet,
            output=args.output,
            dry_run=args.dry_run,
            force=args.force,
        )

    elif args.command == "photos":
        from wago.photos import vincular_fotos_lote

        vincular_fotos_lote(
            args.csv,
            dry_run=args.dry_run,
            force=args.force,
            timeout=args.timeout,
        )

    elif args.command == "ml-map":
        from wago.ml.map_categories import gerar_mapa_geral

        gerar_mapa_geral(
            max_nodes=args.max_nodes,
            resume=not args.no_resume,
        )

    elif args.command == "ml-templates":
        from wago.ml.templates import gerar_planilhas_alvo

        gerar_planilhas_alvo(limit=args.limit)

    elif args.command == "nuvemshop-auth":
        from wago.photos import autenticar_nuvemshop_auto

        autenticar_nuvemshop_auto(timeout_s=args.timeout)

    elif args.command == "taxonomia":
        if args.acao == "inventario":
            from wago.taxonomia.inventario import executar_inventario

            executar_inventario()
        elif args.acao == "diagnostico":
            from wago.taxonomia.inventario import gerar_diagnostico

            gerar_diagnostico()
        elif args.acao == "migrar-decisoes":
            from wago.taxonomia.migrar_decisoes import migrar

            print(json.dumps(migrar(), ensure_ascii=False, indent=2))
        elif args.acao == "gerar":
            from wago.taxonomia.modelo import executar_geracao

            executar_geracao(args.snapshot)
        elif args.acao == "status":
            from wago.taxonomia.modelo import status_taxonomia

            print(json.dumps(status_taxonomia(), ensure_ascii=False, indent=2))
        elif args.acao == "aprovar":
            from wago.taxonomia.modelo import aprovar_taxonomia

            print(f"Taxonomia aprovada: {aprovar_taxonomia(args.por)}")
        elif args.acao == "classificar":
            from wago.taxonomia.classificador import executar_classificacao

            executar_classificacao(sem_ia=args.sem_ia)
        elif args.acao == "plano":
            from wago.taxonomia.plano import executar_plano_dry_run

            executar_plano_dry_run()
        elif args.acao == "aplicar":
            if args.piloto:
                from wago.taxonomia.aplicar import executar_piloto

                executar_piloto()
            else:
                from wago.taxonomia.aplicar import executar_aplicacao_completa

                destino = executar_aplicacao_completa(retomar=args.retomar)
                print(f"Diário da aplicação: {destino}")
        elif args.acao == "reverter":
            from wago.taxonomia.aplicar import conferir_reversao, reverter
            from wago.taxonomia.nuvemshop_api import NuvemshopAPI

            api = NuvemshopAPI.do_env()
            destino = reverter(args.aplicacao, api)
            print(f"Diário da reversão: {destino}")
            checks = conferir_reversao(api, args.aplicacao, destino)
            for c in checks:
                print(f"  conferência #{c['seq']} {c['acao']}: {'OK' if c['ok'] else 'DIVERGENTE'} {c}")
            if not all(c["ok"] for c in checks):
                raise SystemExit("Reversão com divergências: ver diário.")
        elif args.acao == "excluir-orfas":
            from wago.taxonomia.aplicar import excluir_categorias_orfas
            from wago.taxonomia.nuvemshop_api import NuvemshopAPI

            destino = excluir_categorias_orfas(NuvemshopAPI.do_env(), args.categorias)
            print(f"Diário da aplicação: {destino}")
        elif args.acao == "simular":
            from wago.taxonomia import modelo
            from wago.taxonomia.classificador import simular_proposta
            from wago.taxonomia.inventario import carregar_snapshot, ultimo_snapshot

            proposta = modelo.carregar_proposta()
            if not proposta:
                raise SystemExit("Nenhuma proposta pendente para simular.")
            simulada = simular_proposta(proposta, carregar_snapshot(ultimo_snapshot()))
            print(f"Proposta atualizada com a simulação: {modelo.salvar_proposta(simulada)}")
        elif args.acao == "editar-lote":
            from wago.taxonomia import modelo

            edicoes = json.loads(args.arquivo.read_text(encoding="utf-8"))
            try:
                nova = modelo.editar_lote(modelo.base_para_edicao(), edicoes)
            except ValueError as exc:
                raise SystemExit(f"Edição recusada: {exc}") from exc
            destino = modelo.salvar_proposta(nova)
            print(modelo.arvore_markdown(nova))
            print(f"{len(edicoes)} edição(ões) aplicadas. Proposta pendente de aprovação: {destino}")
        elif args.acao == "editar":
            from wago.taxonomia import modelo

            kw = {
                k: v
                for k, v in {
                    "id_alvo": args.id_alvo, "nome": args.nome, "pai": args.pai,
                    "origem": args.origem, "destino": args.destino, "sinonimos": args.sinonimos,
                }.items()
                if v is not None
            }
            try:
                nova = modelo.editar(modelo.base_para_edicao(), args.operacao, **kw)
            except (KeyError, ValueError) as exc:
                raise SystemExit(f"Edição recusada: {exc}") from exc
            print(f"Proposta atualizada: {modelo.salvar_proposta(nova)}")

    elif args.command == "worker":
        from wago.dashboard.worker import main as rodar_worker

        rodar_worker(ocioso_max=args.ocioso)

    elif args.command == "status":
        from wago.state import catalogo

        contagem = catalogo().contagem()
        for chave, valor in contagem.items():
            print(f"{chave:<20} {valor}")

    elif args.command == "dashboard":
        from wago.dashboard.server import servir

        servir(porta=args.port, abrir=args.abrir)

    else:
        parser.error(f"Comando desconhecido: {args.command}")


if __name__ == "__main__":
    main()
