"""Fase 2: validação dos exportadores, imagens e preservação do XLSX."""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wago.fill_ml import fill_ml  # noqa: E402
from wago.fill_nuvemshop import COLUNAS, fill_nuvemshop  # noqa: E402
from wago.images import (  # noqa: E402
    baixar_imagem,
    baixar_imagens_lote,
    diretorio_produto,
    extensao_da_imagem,
)

TEMPLATE_ML = ROOT / "templates" / "Anunciar-ML.xlsx"
JPEG = b"\xff\xd8\xff" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _linha(**extra) -> dict:
    base = {
        "Identificador URL": "conector-221-413-wago",
        "Nome": "Conector de emenda 221-413 Wago",
        "Categorias": "Bornes > Conectores",
        "Preço": "13.13",
        "Peso (kg)": "0.01",
        "Altura (cm)": "2",
        "Largura (cm)": "3",
        "Comprimento (cm)": "4",
        "Estoque": "2",
        "SKU": "221-413",
        "Descrição": "Produto teste",
        "Tags": "221-413, Wago",
        "Título para SEO": "Conector de emenda 221-413 Wago",
        "Descrição para SEO": "Produto teste",
        "Marca": "Wago",
        "Produto Físico": "SIM",
        "MPN (Cód. Exclusivo Modelo Fabricante)": "221-413",
        "Custo": "7.18",
        "Visibilidade": "Visível",
        "Exibir na loja": "SIM",
        "Frete gratis": "NÃO",
        "Fotos": "https://www.wago.com/medias/example.png",
        "IPI": "0.7",
    }
    base.update(extra)
    return base


def _master(tmp: Path, linhas: list[dict]) -> Path:
    path = tmp / "master.csv"
    df = pd.DataFrame(linhas)
    for coluna in COLUNAS:
        if coluna not in df.columns:
            df[coluna] = ""
    df.to_csv(path, sep=";", index=False, encoding="utf-8-sig")
    return path


def _fixa_margem(monkeypatch) -> None:
    monkeypatch.setattr("wago.export.margin_divisor", lambda: 0.6)


class _Resposta:
    def __init__(self, status: int, content: bytes):
        self.status_code = status
        self.content = content


class _Sessao:
    def __init__(self, respostas):
        self._respostas = list(respostas)
        self.chamadas = 0

    def get(self, url, timeout=30):
        self.chamadas += 1
        item = self._respostas.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_extensao_pelo_conteudo():
    assert extensao_da_imagem(JPEG) == ".jpg"
    assert extensao_da_imagem(PNG) == ".png"
    assert extensao_da_imagem(b"<html>" + b"x" * 20) is None
    assert extensao_da_imagem(b"\xff\xd8") is None


def test_nuvemshop_rejeita_ipi_preco_e_duplicata(tmp_path, monkeypatch):
    _fixa_margem(monkeypatch)
    master = _master(
        tmp_path,
        [
            _linha(),
            _linha(SKU="sem-ipi", IPI="", Nome="Sem IPI"),
            _linha(SKU="preco-ruim", Preço="1.00", Nome="Preço ruim"),
            _linha(SKU="221-413", Nome="Repetido"),
            _linha(SKU="medida", **{"Peso (kg)": "abc", "Nome": "Medida"}),
        ],
    )
    out = tmp_path / "ns.csv"
    fill_nuvemshop(master, output=out)
    df = pd.read_csv(out, sep=";", dtype=str)
    assert list(df.columns) == COLUNAS
    assert df["SKU"].tolist() == ["221-413"]
    assert "Shorts" not in df["Nome"].tolist()
    resumo = (tmp_path / "ns.resumo.json").read_text(encoding="utf-8")
    assert "IPI ausente" in resumo
    assert "diverge" in resumo
    assert "SKU duplicado" in resumo
    assert "Peso (kg) inválido" in resumo


def test_nuvemshop_dry_run_e_force(tmp_path, monkeypatch):
    _fixa_margem(monkeypatch)
    master = _master(tmp_path, [_linha()])
    out = tmp_path / "ns.csv"
    assert fill_nuvemshop(master, output=out, dry_run=True) is None
    assert not out.exists()
    fill_nuvemshop(master, output=out)
    with pytest.raises(SystemExit, match="já existe"):
        fill_nuvemshop(master, output=out)
    fill_nuvemshop(master, output=out, force=True)
    assert len(pd.read_csv(out, sep=";")) == 1


def test_nuvemshop_master_vazio(tmp_path):
    master = tmp_path / "vazio.csv"
    pd.DataFrame(columns=["SKU"]).to_csv(master, sep=";", index=False)
    with pytest.raises(SystemExit, match="sem produtos"):
        fill_nuvemshop(master, output=tmp_path / "ns.csv")


def test_imagem_valida_nao_repte_e_url_nao_define_extensao(tmp_path):
    pasta = tmp_path / "221-413__conector"
    sessao = _Sessao([_Resposta(200, JPEG)])
    primeiro = baixar_imagem(
        "https://cdn.example/foto.png",
        pasta,
        1,
        session=sessao,
        tentativas=1,
    )
    assert primeiro["status"] == "baixada"
    assert Path(primeiro["caminho"]).suffix == ".jpg"
    assert sessao.chamadas == 1
    segundo = baixar_imagem(
        "https://cdn.example/foto.png",
        pasta,
        1,
        session=sessao,
        tentativas=1,
    )
    assert segundo["status"] == "ignorada"
    assert sessao.chamadas == 1


def test_imagem_recusa_html_e_retenta_http(tmp_path):
    pasta = tmp_path / "sku"
    ruim = _Sessao([_Resposta(200, b"<html>" + b"x" * 40)])
    falha = baixar_imagem("https://cdn.example/a", pasta, 1, session=ruim, tentativas=1)
    assert falha["status"] == "falha"
    assert list(pasta.glob("*")) == []

    instavel = _Sessao([_Resposta(503, b""), _Resposta(200, PNG)])
    ok = baixar_imagem(
        "https://cdn.example/b.png",
        pasta,
        2,
        session=instavel,
        tentativas=3,
    )
    assert ok["status"] == "baixada"
    assert Path(ok["caminho"]).name == "02.png"
    assert instavel.chamadas == 2


def test_lote_nao_escapa_do_diretorio(tmp_path, monkeypatch):
    _fixa_margem(monkeypatch)
    master = _master(
        tmp_path,
        [_linha(SKU=r"..\..\fora", Fotos="https://cdn.example/a.jpg", Nome="Alvo")],
    )
    sessao = _Sessao([_Resposta(200, JPEG)])
    base = tmp_path / "imagens"
    baixar_imagens_lote(master, output_dir=base, session=sessao, tentativas=1)
    arquivos = [p for p in base.rglob("*") if p.is_file() and p.suffix != ".json"]
    assert arquivos
    assert all(base.resolve() in p.resolve().parents for p in arquivos)
    assert diretorio_produto(base, r"../etc", "x").is_relative_to(base.resolve())


def test_imagens_dry_run_nao_baixa(tmp_path):
    master = _master(tmp_path, [_linha()])
    base = tmp_path / "imagens"
    baixar_imagens_lote(master, output_dir=base, dry_run=True)
    assert not base.exists()


def test_ml_preserva_template_e_rejeita_aba(tmp_path, monkeypatch):
    _fixa_margem(monkeypatch)
    antes = hashlib.sha256(TEMPLATE_ML.read_bytes()).hexdigest()
    master = _master(
        tmp_path,
        [
            _linha(),
            _linha(SKU="sem-ipi", IPI="", Nome="Sem IPI"),
            _linha(),
        ],
    )
    out = tmp_path / "ml.xlsx"
    fill_ml(master, sheet="Outros", output=out)
    assert hashlib.sha256(TEMPLATE_ML.read_bytes()).hexdigest() == antes
    with zipfile.ZipFile(out) as arquivo:
        xml = arquivo.read("xl/worksheets/sheet8.xml")
        ss = arquivo.read("xl/sharedStrings.xml")
        assert b"<dataValidations" in xml
        assert b"<f>" in xml or b"<f " in xml
        assert any(nome.startswith("xl/media/") for nome in arquivo.namelist())
        assert b't="inlineStr"' not in xml
        assert b'r="F8" s="0" t="s"' in xml
        assert b"221-413" in ss
    with pytest.raises(SystemExit, match="não existe"):
        fill_ml(master, sheet="Aba Inexistente", output=tmp_path / "outra.xlsx")
    assert hashlib.sha256(TEMPLATE_ML.read_bytes()).hexdigest() == antes


def test_ml_dry_run_e_aba_conectores(tmp_path, monkeypatch):
    _fixa_margem(monkeypatch)
    master = _master(tmp_path, [_linha()])
    out = tmp_path / "ml.xlsx"
    assert fill_ml(master, output=out, dry_run=True) is None
    assert not out.exists()
    fill_ml(master, sheet="Conectores", output=out)
    with zipfile.ZipFile(out) as arquivo:
        xml = arquivo.read("xl/worksheets/sheet10.xml")
        ss = arquivo.read("xl/sharedStrings.xml")
    assert b't="inlineStr"' not in xml
    assert b'r="H9" s="0" t="s"' in xml or b'r="H9" t="s" s="0"' in xml
    assert b'r="G9" t="s" s="42"' in xml or b'r="G9" s="42" t="s"' in xml
    assert b"221-413" in ss
    assert b"https://www.wago.com/medias/example.png" in ss


def test_roteamento_automatico_abas_ml_e_secoes_wago(tmp_path, monkeypatch):
    from wago.extract import (
        _parse_grupos_classifications,
        limpar_categoria_produto,
        montar_descricao_produto,
    )
    from wago.fill_ml import inferir_aba_ml

    _fixa_margem(monkeypatch)
    grupos = [
        {
            "name": "Observações",
            "features": [
                {
                    "name": "General safety information",
                    "featureUnit": None,
                    "featureValues": [{"value": "<p>Somente eletricista</p>"}],
                }
            ],
            "subClassifications": [],
        },
        {
            "name": "Dados geométricos",
            "features": [
                {
                    "name": "Largura",
                    "featureUnit": {"symbol": "mm"},
                    "featureValues": [{"value": "17,9"}],
                }
            ],
            "subClassifications": [],
        },
    ]
    specs, secoes = _parse_grupos_classifications(grupos)
    assert specs["Largura"] == "17,9 mm"
    desc = montar_descricao_produto(
        sku="890-606",
        nome="Conector de distribuição em T; 3 polos",
        resumo_curto="Conector de distribuição em T",
        descricao_aba="Conector WINSTA MINI.",
        secoes=secoes,
        specs_dict=specs,
    )
    assert "Conector de distribuição em T; 3 polos" in desc
    assert "Código Wago: 890-606 | Marca: Wago" in desc
    assert "[Observações]" not in desc
    assert "[Dados geométricos]" not in desc
    assert "DESCRIÇÃO DO PRODUTO:" not in desc

    assert (
        limpar_categoria_produto(
            "Página inicial > produtos > ... > Marcação > 209-659 > WSB Quick marking system",
            sku="209-659",
        )
        == "Marcação"
    )
    assert (
        limpar_categoria_produto(
            "Página inicial > produtos > ... > Conectores plugáveis > 770-659 > WINSTA® MIDI",
            sku="770-659",
        )
        == "Conectores plugáveis"
    )

    assert (
        inferir_aba_ml(
            {"Nome": "Conector de emenda 221-413 Wago", "Categorias": "Bornes > Conectores"}
        )
        == "Conectores"
    )
    assert (
        inferir_aba_ml(
            {"Nome": "Módulo de relés 788-120 Wago", "Categorias": "Relés e acopladores óticos"}
        )
        == "Relés"
    )
    assert (
        inferir_aba_ml(
            {"Nome": "Cartão de identificação WSB 209-679 Wago", "Categorias": "Marcação"}
        )
        == "Outros"
    )

    master = _master(
        tmp_path,
        [
            _linha(
                SKU="890-606",
                Nome="Conector de distribuição em T 890-606 Wago",
                Categorias="Conectores plugáveis",
                Descrição="• Cor: preto\n• Material de isolamento: Poliamida (PA66)",
            ),
            _linha(
                SKU="788-120",
                Nome="Módulo de relés 788-120 Wago",
                Categorias="Módulos de relés",
                Descrição="• Tensão: 24 VDC",
            ),
            _linha(
                SKU="209-679",
                Nome="Cartão de identificação WSB 209-679 Wago",
                Categorias="Marcação",
                Descrição="• Cor: branco",
            ),
        ],
    )
    ml_out = tmp_path / "ml_auto.xlsx"
    fill_ml(master, sheet="Automático", output=ml_out)

    with zipfile.ZipFile(ml_out) as zf:
        xml_con = zf.read("xl/worksheets/sheet10.xml")
        xml_rel = zf.read("xl/worksheets/sheet9.xml")
        xml_out = zf.read("xl/worksheets/sheet8.xml")
    assert b'r="H9"' in xml_con
    assert b'r="N9" s="150" t="n"><v>3</v>' in xml_con
    assert b'r="H9"' in xml_rel
    assert b'r="F8"' in xml_out
    resumo = json.loads((tmp_path / "ml_auto.resumo.json").read_text(encoding="utf-8"))
    assert resumo["abas_preenchidas"] == {
        "Conectores": 1,
        "Relés": 1,
        "Outros": 1,
    }


def test_specs_dom_e_integridade_ml_medidas(tmp_path, monkeypatch):
    from bs4 import BeautifulSoup
    from wago.extract import _extrair_specs_dom

    _fixa_margem(monkeypatch)
    html = """
    <table>
      <tbody>
        <tr class="bg-light"><td class="attrib">Largura</td><td><span>17,9 mm / 0.705 polegadas</span></td></tr>
        <tr class="bg-secondary-7"><td class="attrib">Altura</td><td><span>34 mm</span></td></tr>
        <tr class="bg-light"><td class="attrib">Profundidade</td><td><span>54 mm</span></td></tr>
        <tr class="bg-light"><td class="attrib">Peso</td><td><span>8 g</span></td></tr>
        <tr class="bg-secondary-7"><td class="attrib">GTIN</td><td><span>4045454490485</span></td></tr>
        <tr class="bg-light"><td class="attrib">Número de tarifa alfandegária</td><td><span>8536.69.90</span></td></tr>
      </tbody>
    </table>
    """
    extraido = _extrair_specs_dom(BeautifulSoup(html, "html.parser"))
    assert extraido["largura_cm"] == 1.79
    assert extraido["altura_cm"] == 3.4
    assert extraido["comprimento_cm"] == 5.4
    assert extraido["peso_kg"] == 0.008
    assert extraido["gtin"] == "4045454490485"
    assert extraido["ncm"] == "8536.69.90"

    master = _master(
        tmp_path,
        [
            _linha(
                **{
                    "Largura (cm)": "",
                    "Altura (cm)": "",
                    "Comprimento (cm)": "",
                    "Peso (kg)": "",
                    "Código de barras": "4045454490485",
                    "Preço": "9.6",
                }
            )
        ],
    )
    monkeypatch.setattr("wago.export.margin_divisor", lambda: (7.18 + 0.7) / 9.6)
    ns_out = tmp_path / "ns.csv"
    ml_out = tmp_path / "ml.xlsx"
    fill_nuvemshop(master, output=ns_out)
    fill_ml(master, sheet="Outros", output=ml_out)

    df_ns = pd.read_csv(ns_out, sep=";", dtype=str).fillna("")
    assert df_ns.iloc[0]["Preço"] == "9.60"
    assert df_ns.iloc[0]["Custo"] == "7.18"
    assert df_ns.iloc[0]["Peso (kg)"] == "0.01"
    assert df_ns.iloc[0]["Altura (cm)"] == "1"
    assert df_ns.iloc[0]["Largura (cm)"] == "1"
    assert df_ns.iloc[0]["Comprimento (cm)"] == "1"
    assert df_ns.iloc[0]["Código de barras"] == "4045454490485"

    with zipfile.ZipFile(ml_out) as zf:
        xml = zf.read("xl/worksheets/sheet8.xml")
    assert b'r="A8" s="36" t="s"' in xml
    assert b'r="E8" s="42" t="s"' in xml
    assert b'r="H8" s="131" t="n"' in xml
    assert b'r="J8" s="132" t="n"><v>1</v>' in xml
    assert b'r="K8" s="133" t="n"><v>1</v>' in xml
    assert b'r="L8" s="134" t="n"><v>1</v>' in xml
    assert b'r="M8" s="135" t="n"><v>0.01</v>' in xml
