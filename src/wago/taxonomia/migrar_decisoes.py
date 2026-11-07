"""Preserva decisões humanas quando os id_alvo mudarem.

O arquivo continua indexado pelo produto: o classificador precisa saber qual
SKU foi decidido. Trocar essa chave pelo caminho apagaria o vínculo do produto.
O que muda é a identidade da categoria. Cada valor passa a guardar o id atual
e o caminho normalizado da folha, da raiz até ela:

    {"9": {"id_alvo": "bornes", "caminho": "material eletrico > borne"}}

Se o id sumir numa taxonomia futura, o classificador acha a folha pelo caminho.
Um backup é gravado antes de qualquer escrita.

Uso: python -m wago taxonomia migrar-decisoes
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from wago.config import TAXONOMIA
from wago.taxonomia.modelo import caminho_estavel


def migrar(pasta: Path | None = None) -> dict:
    pasta = Path(pasta or TAXONOMIA)
    origem = pasta / "decisoes.json"
    if not origem.exists():
        return {"migrados": 0, "backup": None, "aviso": "decisoes.json não existe; nada a migrar"}
    try:
        bruto = json.loads(origem.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"migrados": 0, "backup": None, "aviso": f"decisoes.json ilegível: {exc}"}
    if not isinstance(bruto, dict):
        return {"migrados": 0, "backup": None, "aviso": "decisoes.json não é um objeto"}

    por: dict[str, dict] = {}
    tax_arq = pasta / "taxonomia.json"
    aviso = None
    if tax_arq.exists():
        try:
            tax = json.loads(tax_arq.read_text(encoding="utf-8"))
            por = {n["id_alvo"]: n for n in tax.get("nos") or [] if isinstance(n, dict) and n.get("id_alvo")}
        except (OSError, ValueError, KeyError):
            por = {}
    if not por:
        aviso = "taxonomia.json sem nós; caminhos ficam vazios e o id_alvo é mantido"

    selo = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = pasta / f"decisoes.backup-{selo}.json"
    extra = 0
    while backup.exists():
        extra += 1
        backup = pasta / f"decisoes.backup-{selo}-{extra}.json"
    shutil.copy2(origem, backup)

    saida: dict = {}
    migrados = 0
    for chave, valor in bruto.items():
        if str(chave).startswith("_"):
            saida[chave] = valor
            continue
        if isinstance(valor, str) and valor:
            ident = valor
        elif isinstance(valor, dict) and valor.get("id_alvo"):
            ident = str(valor["id_alvo"])
        else:
            saida[chave] = valor
            continue
        caminho = caminho_estavel(por, ident) if ident in por else ""
        saida[chave] = {"id_alvo": ident, "caminho": caminho}
        migrados += 1

    tmp = origem.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(saida, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(origem)
    return {"migrados": migrados, "backup": str(backup), "aviso": aviso}
