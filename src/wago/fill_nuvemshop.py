"""Etapa 4a: preenche o CSV oficial de importação Nuvemshop."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from wago.config import OUT, ROOT, ensure_dirs
from wago.export import selecionar_para_exportacao
from wago.extract import limpar_categoria_produto, montar_descricao_produto
from wago.images import _slug

TEMPLATE = ROOT / "templates" / "produtos-Nuvemshop.csv"

# Ordem oficial do template anexado
COLUNAS = [
    "Identificador URL",
    "Nome",
    "Categorias",
    "Nome da variação 1",
    "Valor da variação 1",
    "Nome da variação 2",
    "Valor da variação 2",
    "Nome da variação 3",
    "Valor da variação 3",
    "Preço",
    "Preço promocional",
    "Peso (kg)",
    "Altura (cm)",
    "Largura (cm)",
    "Comprimento (cm)",
    "Estoque",
    "SKU",
    "Código de barras",
    "Exibir na loja",
    "Frete gratis",
    "Descrição",
    "Tags",
    "Título para SEO",
    "Descrição para SEO",
    "Marca",
    "Produto Físico",
    "MPN (Cód. Exclusivo Modelo Fabricante)",
    "Sexo",
    "Faixa etária",
    "Custo",
    "Visibilidade",
]


def _carregar_master(master_csv: Path | str) -> pd.DataFrame:
    path = Path(master_csv)
    if not path.exists():
        raise SystemExit(f"Master não encontrado: {path}")
    return pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")


def _cabecalho_template(template: Path) -> list[str]:
    header = pd.read_csv(
        template, sep=";", nrows=0, encoding="utf-8-sig"
    ).columns.tolist()
    if len(header) != 31 or set(header) != set(COLUNAS):
        raise SystemExit(
            "Template Nuvemshop deve ter as 31 colunas oficiais. "
            f"Encontradas {len(header)} em {template}."
        )
    return header


def _medida_invalida(row: dict[str, Any]) -> str | None:
    for campo in ("Peso (kg)", "Altura (cm)", "Largura (cm)", "Comprimento (cm)"):
        raw = str(row.get(campo) or "").strip()
        if raw.lower() in ("", "nan", "none"):
            continue
        try:
            float(raw.replace(",", "."))
        except ValueError:
            return f"{campo} inválido"
    return None


def _publicar_csv(df: pd.DataFrame, dest: Path, header: list[str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    df.to_csv(tmp, sep=";", index=False, encoding="utf-8-sig")
    try:
        check = pd.read_csv(tmp, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"CSV gerado ilegível: {exc}") from exc
    if list(check.columns) != header or len(check) != len(df):
        tmp.unlink(missing_ok=True)
        raise SystemExit("CSV gerado não confere com o cabeçalho oficial.")
    tmp.replace(dest)


def _publicar_resumo(dest: Path, resumo: dict[str, Any]) -> None:
    path = dest.with_suffix(".resumo.json")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _imprimir_resumo(resumo: dict[str, Any]) -> None:
    print(
        "Nuvemshop: "
        f"lidos={resumo['lidos']} exportados={resumo['exportados']} "
        f"rejeitados={resumo['rejeitados']}"
    )
    for item in resumo["motivos"]:
        print(f"  - {item['sku']}: {item['motivo']}")


def _como_zero(raw: Any) -> bool:
    try:
        return float(str(raw).replace(",", ".")) <= 0
    except (TypeError, ValueError):
        return False


def _formatar_moeda(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.lower() in ("", "nan", "none"):
        return ""
    try:
        return f"{float(s.replace(',', '.')):.2f}"
    except ValueError:
        return s


def _limpar_descricao_nuvemshop(raw: Any, sku: str, nome: str) -> str:
    texto = str(raw or "").strip()
    if not texto:
        return montar_descricao_produto(sku=sku, nome=nome)
    # Se veio de um master antigo com blocos de acordeões/Tipo/DESCRIÇÃO DO PRODUTO,
    # extrai apenas a linha de modelo/resumo curto e reconstrói no formato enxuto.
    if any(
        marcador in texto
        for marcador in (
            "DETALHES E ESPECIFICAÇÕES TÉCNICAS:",
            "ESPECIFICAÇÕES TÉCNICAS E CARACTERÍSTICAS:",
            "DESCRIÇÃO DO PRODUTO:",
            "\n\nTipo:",
        )
    ):
        primeira = texto.splitlines()[0].strip()
        primeira = re.sub(r"^Modelo\s*:\s*", "", primeira, flags=re.I).strip()
        return montar_descricao_produto(sku=sku, nome=primeira or nome)
    return texto


def _linha_nuvemshop(row: dict[str, Any], header: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {c: "" for c in header}
    for col in header:
        if col in row and str(row[col]).strip().lower() not in ("", "nan", "none"):
            out[col] = row[col]

    sku = str(out.get("SKU") or row.get("SKU") or "").strip()
    nome = str(out.get("Nome") or row.get("Nome") or "").strip()

    if out.get("Categorias"):
        cat_raw = str(out["Categorias"]).strip()
        cat_limpa = limpar_categoria_produto(cat_raw, sku=sku, nome=nome)
        out["Categorias"] = cat_limpa or cat_raw

    out["Descrição"] = _limpar_descricao_nuvemshop(
        out.get("Descrição", ""), sku=sku, nome=nome
    )

    desc_seo_raw = str(out.get("Descrição para SEO") or out["Descrição"]).strip()
    if any(
        m in desc_seo_raw
        for m in ("\n", "Modelo:", "Tipo:", "DETALHES E ESPECIFICAÇÕES")
    ):
        desc_seo_base = re.sub(r"\s+", " ", out["Descrição"]).strip()
    else:
        desc_seo_base = re.sub(r"\s+", " ", desc_seo_raw).strip()
    out["Descrição para SEO"] = (
        desc_seo_base[:157].rstrip() + "..."
        if len(desc_seo_base) > 160
        else desc_seo_base
    )

    for campo in ("Preço", "Preço promocional", "Custo"):
        if out.get(campo):
            out[campo] = _formatar_moeda(out[campo])

    # Defaults seguros do template
    out.setdefault("Exibir na loja", "SIM")
    if not out.get("Exibir na loja"):
        out["Exibir na loja"] = "SIM"
    if not out.get("Frete gratis"):
        out["Frete gratis"] = "NÃO"
    if not out.get("Produto Físico"):
        out["Produto Físico"] = "SIM"
    if not out.get("Marca"):
        out["Marca"] = "Wago"
    if not out.get("Visibilidade"):
        out["Visibilidade"] = "Visível"
    if not out.get("Estoque"):
        out["Estoque"] = "2"
    if not out.get("Peso (kg)") or _como_zero(out.get("Peso (kg)")):
        out["Peso (kg)"] = "0.01"
    for campo in ("Altura (cm)", "Largura (cm)", "Comprimento (cm)"):
        if not out.get(campo) or _como_zero(out.get(campo)):
            out[campo] = "1"
    if not out.get("MPN (Cód. Exclusivo Modelo Fabricante)"):
        out["MPN (Cód. Exclusivo Modelo Fabricante)"] = out.get("SKU", "")
    if not out.get("Identificador URL"):
        out["Identificador URL"] = _slug(str(out.get("Nome") or out.get("SKU") or ""))
    return out


def fill_nuvemshop(
    master_csv: Path | str,
    *,
    output: Path | str | None = None,
    template: Path | str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> Path | None:
    """
    Gera CSV de importação Nuvemshop a partir do master.

    O cabeçalho e a ordem vêm do template oficial. Linhas de exemplo
    do template não são copiadas. Não substitui um arquivo já existente
    sem ``force``.
    """
    ensure_dirs()
    tpl = Path(template) if template else TEMPLATE
    if not tpl.exists():
        raise SystemExit(
            f"Template Nuvemshop ausente: {tpl}\n"
            "Coloque o arquivo em templates/produtos-Nuvemshop.csv"
        )

    header = _cabecalho_template(tpl)
    df_master = _carregar_master(master_csv)
    if df_master.empty:
        raise SystemExit("Master sem produtos.")

    aceitos, rejeitados = selecionar_para_exportacao(
        df_master.to_dict(orient="records")
    )
    linhas: list[dict[str, Any]] = []
    for row in aceitos:
        medida = _medida_invalida(row)
        if medida:
            rejeitados.append((str(row.get("SKU") or "?"), medida))
            continue
        linhas.append(_linha_nuvemshop(row, header))

    resumo = {
        "origem": str(master_csv),
        "lidos": int(len(df_master)),
        "exportados": len(linhas),
        "rejeitados": len(rejeitados),
        "motivos": [{"sku": sku, "motivo": motivo} for sku, motivo in rejeitados],
    }
    _imprimir_resumo(resumo)
    if not linhas:
        raise SystemExit("Nenhum produto exportado para a Nuvemshop.")
    if dry_run:
        print("Simulação: nenhum arquivo foi gravado.")
        return None

    if output:
        out_path = Path(output)
    else:
        stamp = datetime.now().strftime("%d-%m_%H%M%S")
        out_path = OUT / f"produtos-Nuvemshop-preenchido_{stamp}.csv"
    if out_path.exists() and not force:
        raise SystemExit(
            f"Arquivo já existe: {out_path}. Use --force para substituir."
        )

    df_out = pd.DataFrame(linhas, columns=header)
    _publicar_csv(df_out, out_path, header)
    _publicar_resumo(out_path, resumo)
    print(f"Nuvemshop preenchido: {out_path} ({len(df_out)} produtos)")

    if output is None:
        latest = OUT / "produtos-Nuvemshop-preenchido.csv"
        _publicar_csv(df_out, latest, header)
        print(f"Cópia latest: {latest}")
    return out_path
