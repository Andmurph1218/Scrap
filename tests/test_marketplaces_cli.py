import pytest

from wago.__main__ import build_parser, main


def test_parser_bling_auth_exige_code():
    args = build_parser().parse_args(["bling-auth", "--code", "abc"])
    assert args.command == "bling-auth" and args.code == "abc"


def test_parser_bling_categorias_existe():
    assert build_parser().parse_args(["bling-categorias"]).command == "bling-categorias"


def test_bling_auth_nao_imprime_os_tokens(monkeypatch, capsys):
    class AuthFake:
        def trocar_codigo(self, code):
            assert code == "abc"
            return {"access_token": "SEGREDO", "refresh_token": "SEGREDO2", "expira_em": 1760000000}

    monkeypatch.setattr("wago.marketplaces.bling_auth.BlingAuth", lambda: AuthFake())
    main(["bling-auth", "--code", "abc"])
    saida = capsys.readouterr().out
    assert "SEGREDO" not in saida and "Token do Bling salvo" in saida


def test_bling_categorias_lista_id_nome_e_pai(monkeypatch, capsys):
    class ProviderFake:
        def listar_categorias(self):
            return [{"id": 1, "nome": "Automação", "pai_id": None, "descricao": ""},
                    {"id": 2, "nome": "CLPs", "pai_id": 1, "descricao": ""}]

    monkeypatch.setattr("wago.marketplaces.criar_provider", lambda nome: ProviderFake())
    main(["bling-categorias"])
    saida = capsys.readouterr().out
    assert "1\tAutomação" in saida and "2\tCLPs\tpai=1" in saida and "2 categoria(s)" in saida


def test_bling_auth_erro_de_autenticacao_vira_system_exit(monkeypatch, capsys):
    from wago.marketplaces.base import ErroAutenticacao

    class AuthFake:
        def trocar_codigo(self, code):
            raise ErroAutenticacao(401, "código inválido")

    monkeypatch.setattr("wago.marketplaces.bling_auth.BlingAuth", lambda: AuthFake())
    with pytest.raises(SystemExit, match="código inválido"):
        main(["bling-auth", "--code", "abc"])


def test_bling_sincronizar_taxonomia_help():
    from wago.__main__ import build_parser
    import sys
    from io import StringIO

    parser = build_parser()

    # Capture help output
    old_stdout = sys.stdout
    sys.stdout = StringIO()
    try:
        parser.parse_args(["bling-sincronizar-taxonomia", "--help"])
    except SystemExit:
        pass
    output = sys.stdout.getvalue()
    sys.stdout = old_stdout

    assert "--dry-run" in output
    assert "--taxonomia" in output
    assert "--estado" in output


# ---------------------------------------------------------------- Tests para tax-aplicar --provider

def test_parser_tax_aplicar_provider_required_args():
    """O subparser tax-aplicar exige --provider."""
    parser = build_parser()
    args = parser.parse_args(["taxonomia", "tax-aplicar", "--provider", "bling"])
    assert args.command == "taxonomia"
    assert args.acao == "tax-aplicar"
    assert args.provider == "bling"
    assert args.dry_run is False


def test_parser_tax_aplicar_provider_dry_run():
    """tax-aplicar aceita --dry-run."""
    parser = build_parser()
    args = parser.parse_args(["taxonomia", "tax-aplicar", "--provider", "nuvemshop", "--dry-run"])
    assert args.command == "taxonomia"
    assert args.acao == "tax-aplicar"
    assert args.provider == "nuvemshop"
    assert args.dry_run is True


def test_parser_tax_aplicar_provider_invalid():
    """Provider inválido deve falhar no parse."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["taxonomia", "tax-aplicar", "--provider", "invalid"])


def test_parser_tax_aplicar_provider_choices():
    """Apenas 'bling' e 'nuvemshop' são válidos."""
    parser = build_parser()
    # bling é válido
    args = parser.parse_args(["taxonomia", "tax-aplicar", "--provider", "bling"])
    assert args.provider == "bling"
    # nuvemshop é válido
    args = parser.parse_args(["taxonomia", "tax-aplicar", "--provider", "nuvemshop"])
    assert args.provider == "nuvemshop"


def test_tax_aplicar_bling_dry_run_no_plano(monkeypatch, capsys):
    """dry-run para Bling sem plano gerador deve falhar com mensagem clara."""

    # Mock: nenhum plano existente - o glob vazio retorna lista vazia
    import tempfile
    from pathlib import Path

    temp_dir = Path(tempfile.mkdtemp())
    monkeypatch.setattr("wago.taxonomia.modelo.TAXONOMIA", temp_dir)

    with pytest.raises(SystemExit, match="Nenhum plano encontrado"):
        main(["taxonomia", "tax-aplicar", "--provider", "bling", "--dry-run"])


def test_tax_aplicar_bling_dry_run_with_plano(monkeypatch, capsys, tmp_path):
    """dry-run para Bling lista as ações do plano."""
    import json
    from pathlib import Path

    # Cria um plano dummy (sem reparentar - Bling não suporta)
    plano_dummy = {
        "versao": 1,
        "gerado_em": "2026-11-19T10:00:00",
        "fases": {
            "criar": [{"seq": 1, "acao": "criar", "nome": "Teste", "pai": None}],
            "renomear": [],
            "reparentar": [],
            "mover": [],
            "arquivar": [],
            "verificar": [],
            "excluir": [],
        }
    }

    # Mock do caminho TAXONOMIA/ planos
    planos_dir = tmp_path / "planos"
    planos_dir.mkdir()
    plano_file = planos_dir / "plano-20261119100000.json"
    plano_file.write_text(json.dumps(plano_dummy), encoding="utf-8")

    # Mock da TAXONOMIA para apontar para nosso diretório temporário
    monkeypatch.setattr("wago.taxonomia.modelo.TAXONOMIA", tmp_path)

    # Dry-run deve executar sem levantar exceção (mas falha se taxonomia não aprovada)
    main(["taxonomia", "tax-aplicar", "--provider", "bling", "--dry-run"])

    # O dry-run deve mostrar a simulação
    saida = capsys.readouterr().out
    assert "[DRY-RUN]" in saida


def test_tax_aplicar_bling_valida_reparentar(monkeypatch, capsys):
    """Validador rejeita reparentar para Bling (por design)."""
    from wago.taxonomia.plano_validator import validar_plano

    # Plano com reparentar (não suportado por Bling)
    plano_com_reparentar = {
        "versao": 1,
        "gerado_em": "2026-11-19T10:00:00",
        "fases": {
            "criar": [],
            "renomear": [],
            "reparentar": [{"seq": 1, "acao": "reparentar", "categoria": 1, "para": 2}],
            "mover": [],
            "arquivar": [],
            "verificar": [],
            "excluir": [],
        }
    }

    # Verifica que validar_plano rejeita reparentar para bling
    with pytest.raises(ValueError) as exc_info:
        validar_plano(plano_com_reparentar, "bling")

    assert "reparentar" in str(exc_info.value).lower() or "suportado" in str(exc_info.value).lower()


def test_tax_aplicar_bling_dry_run_rejeita_reparentar(monkeypatch, capsys, tmp_path):
    """dry-run para Bling rejeita plano com reparentar."""
    import json
    from pathlib import Path

    # Plano com reparentar (não suportado por Bling)
    plano_com_reparentar = {
        "versao": 1,
        "gerado_em": "2026-11-19T10:00:00",
        "fases": {
            "criar": [],
            "renomear": [],
            "reparentar": [{"seq": 1, "acao": "reparentar", "categoria": 1, "para": 2}],
            "mover": [],
            "arquivar": [],
            "verificar": [],
            "excluir": [],
        }
    }

    # Mock do caminho TAXONOMIA/ planos
    planos_dir = tmp_path / "planos"
    planos_dir.mkdir()
    plano_file = planos_dir / "plano-teste.json"
    plano_file.write_text(json.dumps(plano_com_reparentar), encoding="utf-8")

    monkeypatch.setattr("wago.taxonomia.modelo.TAXONOMIA", tmp_path)

    with pytest.raises(SystemExit, match="Plano inválido.*reparentar"):
        main(["taxonomia", "tax-aplicar", "--provider", "bling", "--dry-run"])
