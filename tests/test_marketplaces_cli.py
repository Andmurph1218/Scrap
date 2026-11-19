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
