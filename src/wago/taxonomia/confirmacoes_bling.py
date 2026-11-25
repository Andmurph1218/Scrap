from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from wago.taxonomia import modelo


def _categorias(valores):
    return sorted({int(v["id"] if isinstance(v, dict) else v) for v in valores})


@contextmanager
def _banco():
    modelo.TAXONOMIA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(modelo.TAXONOMIA / "confirmacoes-bling.sqlite", timeout=30)
    try:
        with con:
            con.execute("CREATE TABLE IF NOT EXISTS confirmacoes (produto INTEGER PRIMARY KEY, categorias TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS importados (arquivo TEXT PRIMARY KEY, assinatura TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS acoes_importadas (arquivo TEXT, seq INTEGER, PRIMARY KEY (arquivo, seq))")
            con.execute("CREATE TABLE IF NOT EXISTS reabertos (produto INTEGER PRIMARY KEY, dados TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS progresso_planos (conta TEXT, plano TEXT, seq INTEGER, item TEXT NOT NULL, PRIMARY KEY (conta, plano, seq))")
            yield con
    finally:
        con.close()


def carregar_progresso(conta: str, plano: str) -> list[dict]:
    with _banco() as con:
        return [json.loads(item) for (item,) in con.execute("SELECT item FROM progresso_planos WHERE conta=? AND plano=? ORDER BY seq", (conta, plano))]


def salvar_progresso(conta: str, plano: str, item: dict) -> None:
    with _banco() as con:
        con.execute("INSERT OR REPLACE INTO progresso_planos VALUES (?, ?, ?, ?)", (conta, plano, item["seq"], json.dumps(item, ensure_ascii=False)))


def confirmar(produto: int, categorias: list) -> None:
    with _banco() as con:
        con.execute("INSERT OR REPLACE INTO confirmacoes VALUES (?, ?)", (int(produto), json.dumps(_categorias(categorias))))
        con.execute("DELETE FROM reabertos WHERE produto=?", (int(produto),))


def _importar_diarios(con):
    from wago.taxonomia.motor_universal import carregar_diario_agnostico

    for arquivo in sorted((modelo.TAXONOMIA / "aplicacoes").glob("aplicacao-*.json")):
        lateral = arquivo.with_suffix(".acoes.jsonl")
        assinatura = str((arquivo.stat().st_mtime_ns, lateral.stat().st_size if lateral.exists() else 0))
        anterior = con.execute("SELECT assinatura FROM importados WHERE arquivo=?", (arquivo.name,)).fetchone()
        if anterior and anterior[0] == assinatura:
            continue
        try:
            diario = carregar_diario_agnostico(arquivo)
        except (OSError, json.JSONDecodeError):
            continue
        if diario.get("provider") != "bling":
            continue
        plano_path = modelo.TAXONOMIA / "planos" / str(diario.get("plano") or "")
        if not plano_path.is_file():
            continue
        plano = json.loads(plano_path.read_text(encoding="utf-8"))
        acoes = {a["seq"]: a for fase in plano.get("fases", {}).values() for a in fase}
        for item in diario["acoes"]:
            acao = acoes.get(item.get("seq"), {})
            if item.get("status") == "ok" and acao.get("acao") == "mover_produto":
                categorias = (item.get("depois") or {}).get("categorias") or acao.get("para") or []
                if isinstance(categorias, list) and categorias and all(isinstance(c, int) for c in categorias):
                    nova = con.execute("INSERT OR IGNORE INTO acoes_importadas VALUES (?, ?)", (arquivo.name, item["seq"]))
                    if nova.rowcount:
                        con.execute("INSERT OR IGNORE INTO confirmacoes VALUES (?, ?)", (int(acao["produto"]), json.dumps(_categorias(categorias))))
        con.execute("INSERT OR REPLACE INTO importados VALUES (?, ?)", (arquivo.name, assinatura))


def confirmados() -> dict[int, list[int]]:
    with _banco() as con:
        _importar_diarios(con)
        return {p: json.loads(c) for p, c in con.execute("SELECT produto, categorias FROM confirmacoes")}


def reabertos() -> list[dict]:
    with _banco() as con:
        return [json.loads(d) for (d,) in con.execute("SELECT dados FROM reabertos")]


def reconciliar(produtos: list[dict]) -> None:
    atuais = {int(p["id"]): p for p in produtos}
    with _banco() as con:
        _importar_diarios(con)
        for produto, dados in con.execute("SELECT produto, dados FROM reabertos").fetchall():
            esperado = json.loads(dados).get("categorias_esperadas")
            if produto in atuais and esperado and _categorias(atuais[produto].get("categorias") or []) == esperado:
                con.execute("INSERT OR REPLACE INTO confirmacoes VALUES (?, ?)", (produto, json.dumps(esperado)))
                con.execute("DELETE FROM reabertos WHERE produto=?", (produto,))
        for produto, categorias in con.execute("SELECT produto, categorias FROM confirmacoes").fetchall():
            atual = atuais.get(produto)
            if atual is None or _categorias(atual.get("categorias") or []) != json.loads(categorias):
                con.execute("DELETE FROM confirmacoes WHERE produto=?", (produto,))
                if atual is not None:
                    item = {"id": produto, "nome": atual.get("nome") or str(produto),
                            "skus": atual.get("skus") or ([atual["sku"]] if atual.get("sku") else []),
                            "status": "revisao", "origem": "inventario", "confianca": 0,
                            "categorias_esperadas": json.loads(categorias),
                            "motivo": "Inventário: atribuição anterior não está mais presente no Bling."}
                    con.execute("INSERT OR REPLACE INTO reabertos VALUES (?, ?)", (produto, json.dumps(item)))
