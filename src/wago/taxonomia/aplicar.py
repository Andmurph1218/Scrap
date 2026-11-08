"""Fase 5 — Aplicação do plano na Nuvemshop (escrita) e reversão.

Garantias:
- Snapshot completo da loja antes de qualquer escrita (referência do rollback, inclui `parent_id`).
- Estado "antes" de cada entidade é lido AO VIVO da API e gravado no diário antes da mudança.
- Ordem: criar → renomear → reparentar → mover → arquivar → verificar → excluir.
- `excluir` só depois de confirmar na API: 0 produtos e 0 subcategorias. 404 no DELETE = idempotente.
- `criar` com HTTP 422 reutiliza a categoria existente (e o rollback não a exclui).
- Diário gravado após cada ação: interrupção não perde o rastro; `reverter` restaura a árvore antes dos produtos.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from wago.taxonomia.classificador import TEXTO_REVISAO_CADASTRAL
from wago.taxonomia.nuvemshop_api import ErroNuvemshop, NuvemshopAPI, texto_i18n

ORDEM = ("criar", "renomear", "reparentar", "mover", "arquivar", "verificar", "excluir")
# A Nuvemshop reinsere, no PUT do produto, os ancestrais da árvore que existe naquele instante.
# A reversão restaura a árvore antes de regravar produtos e só apaga a categoria criada no fim.
ORDEM_REVERSAO = ("excluir", "reparentar", "renomear", "arquivar", "mover", "verificar", "criar")
ACAO_FASE = {"criar": "criar", "renomear": "renomear", "reparentar": "reparentar", "mover_produto": "mover",
             "arquivar_produto": "arquivar", "verificar_vazia": "verificar", "excluir": "excluir"}


def pasta_aplicacoes() -> Path:
    from wago.taxonomia import modelo

    return modelo.TAXONOMIA / "aplicacoes"


def _ids(categorias: list) -> list[int]:
    return sorted(int(c["id"] if isinstance(c, dict) else c) for c in categorias or [])


def _visibilidade(prod: dict) -> dict:
    """Estado de publicação para restaurar. Preferimos `visibility` quando a loja expõe o campo."""
    if prod.get("visibility"):
        return {"visibility": prod["visibility"]}
    return {"published": bool(prod.get("published"))}


class Diario:
    """Cabeçalho em `<nome>.json` + uma linha por ação em `<nome>.acoes.jsonl` (append, à prova de queda)."""

    def __init__(self, caminho: Path, cabecalho: dict) -> None:
        self.caminho = caminho
        self.jsonl = caminho.with_suffix(".acoes.jsonl")
        self.dados = {**cabecalho, "refs": {}, "acoes_arquivo": self.jsonl.name}
        self.acoes: list[dict] = []
        caminho.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl.write_text("", encoding="utf-8")
        self.gravar()

    def registrar(self, item: dict) -> None:
        self.acoes.append(item)
        with self.jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        if len(self.acoes) % 25 == 0 or item.get("acao") == "criar":
            self.gravar()

    def gravar(self) -> None:
        tmp = self.caminho.with_suffix(".tmp")
        tmp.write_text(json.dumps({**self.dados, "registradas": len(self.acoes)}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.caminho)


def carregar_diario(arquivo: Path) -> dict:
    """Cabeçalho + ações (JSONL atual ou lista `acoes` dos diários antigos do piloto)."""
    dados = json.loads(Path(arquivo).read_text(encoding="utf-8"))
    jsonl = Path(arquivo).with_name(dados["acoes_arquivo"]) if dados.get("acoes_arquivo") else None
    if jsonl and jsonl.exists():
        dados["acoes"] = [json.loads(linha) for linha in jsonl.read_text(encoding="utf-8").splitlines() if linha.strip()]
    dados.setdefault("acoes", [])
    return dados


# ---------------------------------------------------------------- seleção do piloto
def selecionar_piloto(plano: dict, snapshot: dict, *, preferir_criar: str = "canaleta",
                      preferir_arquivar: str = "sulfite") -> list[dict]:
    """1 criar, 2 mover (1 para a categoria criada, se houver), 1 reparentar de subcategoria com produtos,
    1 arquivar (fora do ramo) e 1 excluir de categoria vazia, com a verificação correspondente."""
    fases = plano["fases"]
    contagem = Counter(c for p in snapshot["produtos"] for c in p.get("categorias") or [])
    filhos = {c["parent_id"] for c in snapshot["categorias"] if c["parent_id"]}

    criar = next((a for a in fases["criar"] if preferir_criar in a["nome"].lower()), None) or fases["criar"][0]
    termo = criar["nome"].split()[0].lower()[:6]  # ex.: "canale" — produto coerente com a categoria criada
    para_nova = [a for a in fases["mover"] if criar["ref"] in a["para"]]
    mov_novo = next((a for a in para_nova if termo in (a.get("nome") or "").lower()), None) \
        or (para_nova[0] if para_nova else None)
    mov_outro = next((a for a in fases["mover"] if all(isinstance(x, int) for x in a["para"])
                      and a is not mov_novo), None)
    reparentar = next((a for a in fases["reparentar"] if contagem.get(a["categoria"], 0) > 0
                       and a["categoria"] not in filhos and isinstance(a["para"], int)), None)
    arquivar = next((a for a in fases["arquivar"] if a.get("fora_do_ramo")
                     and preferir_arquivar in (a["nome"] or "").lower()), None) \
        or next((a for a in fases["arquivar"] if a.get("fora_do_ramo")), None)
    excluir = next((a for a in fases["excluir"] if a["motivo"] == "vazia" and not contagem.get(a["categoria"])
                    and a["categoria"] not in filhos), None)
    verificar = next((a for a in fases["verificar"] if excluir and a["categoria"] == excluir["categoria"]), None)
    escolhidas = [a for a in (criar, reparentar, mov_novo, mov_outro, arquivar, verificar, excluir) if a]
    return sorted(escolhidas, key=lambda a: a["seq"])


# ---------------------------------------------------------------- aplicação
def aplicar_acoes(
    api: NuvemshopAPI, acoes: list[dict], *, plano_arquivo: str, snapshot_arquivo: str,
    log: Callable[[str], None] = print, cancelado: Callable[[], bool] | None = None,
    destino: Path | None = None, antes_produtos: dict[int, dict] | None = None,
    retomar: Path | None = None, progresso_cada: int = 0,
) -> Path:
    """Executa ações isoladamente: falha (429/5xx esgotados, 422, verificação) vira registro e segue."""
    import time

    destino = destino or pasta_aplicacoes() / f"aplicacao-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    diario = Diario(destino, {"tipo": "aplicacao", "iniciada_em": datetime.now().isoformat(timespec="seconds"),
                              "store_id": api.store_id, "plano": plano_arquivo, "snapshot_antes": snapshot_arquivo,
                              "status": "em_andamento", "total_acoes": len(acoes),
                              "retomada_de": retomar.name if retomar else None})
    refs: dict[str, int] = diario.dados["refs"]
    feitas: set[int] = set()
    if retomar:
        anterior = carregar_diario(retomar)
        refs.update(anterior.get("refs") or {})
        feitas = {a["seq"] for a in anterior["acoes"] if a.get("status") == "ok"}
        log(f"Retomando de {retomar.name}: {len(feitas)} ação(ões) já concluídas serão puladas.")
    verificacoes_falhas: set[int] = set()
    inicio = time.monotonic()

    def resolver(valor: Any) -> Any:
        if isinstance(valor, list):
            return [resolver(v) for v in valor]
        if isinstance(valor, str) and valor.startswith("novo:"):
            if valor not in refs:
                raise ErroNuvemshop(0, f"referência {valor} ainda não criada")
            return refs[valor]
        return valor

    ok = falhas = puladas = 0
    por_fase: Counter = Counter()
    # verificar_vazia roda colada à sua exclusão (filhas antes dos pais): verificar tudo antes de excluir
    # bloqueava os pais, que ainda tinham as filhas (visto na aplicação de 05/11/2026)
    seq_excluir = {a["categoria"]: a["seq"] for a in acoes if a["acao"] == "excluir"}

    def chave(a: dict) -> tuple:
        if a["acao"] == "verificar_vazia" and a["categoria"] in seq_excluir:
            return (ORDEM.index("excluir"), seq_excluir[a["categoria"]], 0)
        return (ORDEM.index(ACAO_FASE[a["acao"]]), a["seq"], 1)

    ordenadas = sorted(acoes, key=chave)
    for n, acao in enumerate(ordenadas, 1):
        if cancelado and cancelado():
            diario.dados["status"] = "cancelada"
            log("Cancelamento pedido: parando após a ação atual (o diário permite retomar).")
            break
        if acao["seq"] in feitas:
            continue
        item: dict[str, Any] = {"seq": acao["seq"], "acao": acao["acao"], "em": datetime.now().isoformat(timespec="seconds")}
        if acao["acao"] == "excluir" and acao["categoria"] in verificacoes_falhas:
            item.update(status="pulada", erro="verificação de categoria vazia falhou: exclusão não executada")
            puladas += 1
        else:
            try:
                item.update(_executar(api, acao, resolver, refs, antes_produtos))
                item["status"] = "ok"
                ok += 1
            except Exception as exc:  # noqa: BLE001 — uma ação nunca derruba o lote
                item.update(status="falha", erro=f"{type(exc).__name__}: {exc}"[:300])
                falhas += 1
                if acao["acao"] == "verificar_vazia":
                    verificacoes_falhas.add(acao["categoria"])
        por_fase[f"{ACAO_FASE[acao['acao']]}:{item['status']}"] += 1
        diario.registrar(item)
        if item["status"] != "ok" or not progresso_cada:
            log(f"  #{acao['seq']} {acao['acao']}: {item['status']}"
                + (f" — {item.get('resumo')}" if item.get("resumo") and item["status"] == "ok" else "")
                + (f" — {item.get('erro')}" if item.get("erro") else ""))
        if progresso_cada and (n % progresso_cada == 0 or n == len(ordenadas)):
            decorrido = time.monotonic() - inicio
            ritmo = n / max(decorrido, 1)
            restante = (len(ordenadas) - n) / max(ritmo, 0.01)
            log(f"Progresso: {n}/{len(ordenadas)} ({100 * n / len(ordenadas):.1f}%) · ok {ok} · falhas {falhas}"
                f" · puladas {puladas} · fase {ACAO_FASE[acao['acao']]} · ~{restante / 60:.0f} min restantes")
            diario.dados["progresso"] = {"feitas": n, "ok": ok, "falhas": falhas, "puladas": puladas}
            diario.gravar()
    diario.dados.update(status=diario.dados["status"] if diario.dados["status"] == "cancelada"
                        else ("concluida" if not falhas and not puladas else "concluida_com_falhas"),
                        concluida_em=datetime.now().isoformat(timespec="seconds"), ok=ok, falhas=falhas,
                        puladas=puladas, por_fase=dict(por_fase))
    diario.gravar()
    return destino


def _executar(api: NuvemshopAPI, acao: dict, resolver: Callable, refs: dict,
              antes_produtos: dict[int, dict] | None = None) -> dict:
    tipo = acao["acao"]
    if tipo == "criar":
        cat, reutilizada = api.criar_categoria(acao["nome"], resolver(acao.get("pai")))
        refs[acao["ref"]] = int(cat["id"])
        return {"categoria": int(cat["id"]), "ref": acao["ref"], "reutilizada": reutilizada,
                "depois": {"nome": texto_i18n(cat.get("name")), "parent": cat.get("parent"),
                           "handle": texto_i18n(cat.get("handle"))},
                "resumo": f"'{acao['nome']}' → id {cat['id']}{' (já existia: reutilizada)' if reutilizada else ''}"}
    if tipo in ("renomear", "reparentar"):
        cid = acao["categoria"]
        antes = api.obter_categoria(cid)
        campos = {"name": {**(antes.get("name") or {}), "pt": acao["nome"]}} if tipo == "renomear" \
            else {"parent": resolver(acao["para"])}
        api.atualizar_categoria(cid, **campos)
        depois = api.obter_categoria(cid)  # a resposta do PUT pode vir defasada: confere por GET
        if tipo == "reparentar" and (depois.get("parent") or None) != campos["parent"]:
            raise ErroNuvemshop(409, f"pai não aplicado (esperado {campos['parent']}, atual {depois.get('parent')})")
        if not texto_i18n(depois.get("name")):
            raise ErroNuvemshop(409, "categoria ficou sem nome após o PUT")
        estado = lambda c: {"nome": texto_i18n(c.get("name")), "name": c.get("name"),  # noqa: E731
                            "parent": c.get("parent") or None, "handle": texto_i18n(c.get("handle"))}
        a, d = estado(antes), estado(depois)
        return {"categoria": cid, "antes": a, "depois": d, "handle_preservado": a["handle"] == d["handle"],
                "resumo": (f"pai {a['parent']} → {d['parent']}" if tipo == "reparentar" else f"{a['nome']} → {d['nome']}")
                + f" · handle {'mantido' if a['handle'] == d['handle'] else 'ALTERADO: ' + str(a['handle']) + ' → ' + str(d['handle'])}"}
    if tipo == "mover_produto":
        pid = acao["produto"]
        # estado anterior: snapshot tirado imediatamente antes da aplicação (evita 1 GET por produto)
        antes = antes_produtos.get(pid) if antes_produtos and pid in antes_produtos else api.obter_produto(pid)
        para = resolver(acao["para"])
        depois = api.atualizar_produto(pid, categories=para)
        if set(para) - set(_ids(depois.get("categories"))):
            depois = api.obter_produto(pid)  # resposta do PUT pode vir defasada: confirma por GET
        faltando = set(para) - set(_ids(depois.get("categories")))
        if faltando:
            raise ErroNuvemshop(409, f"categorias não aplicadas: {sorted(faltando)}")
        return {"produto": pid, "antes": {"categories": _ids(antes.get("categories"))},
                "depois": {"categories": _ids(depois.get("categories"))},
                "resumo": f"{_ids(antes.get('categories'))} → {_ids(depois.get('categories'))}"}
    if tipo == "arquivar_produto":
        pid = acao["produto"]
        antes = antes_produtos.get(pid) if antes_produtos and pid in antes_produtos else api.obter_produto(pid)
        payload = {**acao["payload"]}
        if "categories" in payload:
            payload["categories"] = resolver(payload["categories"])
        try:
            depois = api.atualizar_produto(pid, **payload)
            usado = "visibility"
        except ErroNuvemshop as exc:
            if exc.status != 422:
                raise
            fallback = {**acao["payload_fallback"]}
            if "categories" in fallback:
                fallback["categories"] = resolver(fallback["categories"])
            depois = api.atualizar_produto(pid, **fallback)
            usado = "published"
        if depois.get("published") is not False:
            depois = api.obter_produto(pid)
        if depois.get("published"):
            raise ErroNuvemshop(409, "produto continua publicado após o arquivamento")
        resumo = f"{_visibilidade(antes)} → {_visibilidade(depois)}"
        if acao.get("motivo") == TEXTO_REVISAO_CADASTRAL:
            resumo = f"{TEXTO_REVISAO_CADASTRAL} · {resumo}"
        return {"produto": pid, "campo": usado, "motivo": acao.get("motivo"),
                "antes": {**_visibilidade(antes), "categories": _ids(antes.get("categories"))},
                "depois": {**_visibilidade(depois), "categories": _ids(depois.get("categories"))},
                "resumo": resumo}
    if tipo == "verificar_vazia":
        cid = acao["categoria"]
        produtos = api.produtos_da_categoria(cid)
        cat = api.obter_categoria(cid)
        subs = cat.get("subcategories") or []
        if produtos or subs:
            raise ErroNuvemshop(409, f"categoria não está vazia: {len(produtos)} produto(s), {len(subs)} subcategoria(s)")
        return {"categoria": cid, "produtos": 0, "subcategorias": 0, "resumo": "vazia confirmada na API"}
    if tipo == "excluir":
        cid = acao["categoria"]
        try:
            antes = api.obter_categoria(cid)
        except ErroNuvemshop as exc:
            if exc.status == 404:
                return {"categoria": cid, "ja_excluida": True, "resumo": "já não existia (404)"}
            raise
        if api.produtos_da_categoria(cid) or antes.get("subcategories"):
            raise ErroNuvemshop(409, "exclusão bloqueada: a categoria voltou a ter produtos/subcategorias")
        api.excluir_categoria(cid)
        return {"categoria": cid, "antes": antes, "resumo": f"'{texto_i18n(antes.get('name'))}' excluída"}
    raise ErroNuvemshop(0, f"ação desconhecida: {tipo}")


# ---------------------------------------------------------------- reversão
def reverter(arquivo: Path, api: NuvemshopAPI, *, log: Callable[[str], None] = print) -> Path:
    """Desfaz as ações `ok` de um diário.

    A árvore volta antes dos produtos: a Nuvemshop reinsere os ancestrais vigentes no PUT,
    então regravar o produto com a categoria ainda no pai novo cola de volta a categoria criada.
    Categorias excluídas são recriadas (nome, pai, SEO, descrição) e ganham ID novo; o mapa
    `id_antigo → id_novo` religa produtos e subcategorias.
    """
    dados = carregar_diario(Path(arquivo))
    destino = Path(arquivo).with_name(Path(arquivo).stem.replace("aplicacao", "reversao") + ".json")
    diario = Diario(destino, {"tipo": "reversao", "de": Path(arquivo).name, "store_id": api.store_id,
                              "iniciada_em": datetime.now().isoformat(timespec="seconds"), "status": "em_andamento"})
    mapa: dict[int, int] = {}
    diario.dados["mapa_ids"] = mapa
    remap = lambda ids: [mapa.get(i, i) for i in ids]  # noqa: E731
    ok = falhas = 0
    aplicadas = [a for a in dados["acoes"] if a.get("status") == "ok"]
    for acao in sorted(aplicadas, key=lambda a: (ORDEM_REVERSAO.index(ACAO_FASE[a["acao"]]), -a["seq"])):
        item: dict[str, Any] = {"seq": acao["seq"], "acao": f"reverter_{acao['acao']}"}
        try:
            tipo = acao["acao"]
            if tipo == "excluir" and not acao.get("ja_excluida"):
                antes = acao["antes"]
                extra = {}
                for k in ("description", "seo_title", "seo_description", "handle"):
                    v = antes.get(k)
                    v = {i: t for i, t in v.items() if t} if isinstance(v, dict) else v
                    if v:
                        extra[k] = v
                pai = antes.get("parent") or None
                cat, reutilizada = api.criar_categoria(texto_i18n(antes.get("name")), mapa.get(pai, pai), **extra)
                mapa[int(acao["categoria"])] = int(cat["id"])
                item["resumo"] = (f"recriada '{texto_i18n(antes.get('name'))}' id {acao['categoria']} → {cat['id']}"
                                  f" · handle {texto_i18n(cat.get('handle'))}")
                for pid, cats_antes in (acao.get("produtos_antes") or {}).items():  # exclusão de órfã
                    api.atualizar_produto(int(pid), categories=remap(cats_antes))
                if acao.get("produtos_antes"):
                    item["resumo"] += f" · {len(acao['produtos_antes'])} produto(s) religados"
            elif tipo == "arquivar_produto":
                ant = acao["antes"]
                vis = {k: ant[k] for k in ("visibility", "published") if k in ant}
                esperado = sorted(remap(ant["categories"]))
                api.atualizar_produto(acao["produto"], categories=remap(ant["categories"]), **vis)
                depois = api.obter_produto(acao["produto"])
                atual = _ids(depois.get("categories"))
                if atual != esperado:
                    raise ErroNuvemshop(409, f"categorias não restauradas (esperado {esperado}, atual {atual})")
                item["resumo"] = f"restaurado {_visibilidade(depois)} · categorias {atual}"
            elif tipo == "mover_produto":
                esperado = sorted(remap(acao["antes"]["categories"]))
                api.atualizar_produto(acao["produto"], categories=remap(acao["antes"]["categories"]))
                depois = api.obter_produto(acao["produto"])
                atual = _ids(depois.get("categories"))
                if atual != esperado:
                    raise ErroNuvemshop(409, f"categorias não restauradas (esperado {esperado}, atual {atual})")
                item["resumo"] = f"categorias restauradas {atual}"
            elif tipo == "reparentar":
                pai = acao["antes"]["parent"]
                api.atualizar_categoria(acao["categoria"], parent=mapa.get(pai, pai))
                depois = api.obter_categoria(acao["categoria"])
                item["resumo"] = f"pai restaurado → {depois.get('parent')} · nome '{texto_i18n(depois.get('name'))}'"
            elif tipo == "renomear":
                api.atualizar_categoria(acao["categoria"], name=acao["antes"]["name"])
                item["resumo"] = f"nome restaurado → {acao['antes']['nome']}"
            elif tipo == "criar" and not acao.get("reutilizada"):
                cid = acao["categoria"]
                if api.produtos_da_categoria(cid):
                    raise ErroNuvemshop(409, "categoria criada ainda tem produtos; não excluída")
                if api.obter_categoria(cid).get("subcategories"):
                    raise ErroNuvemshop(409, "categoria criada ainda tem subcategorias; não excluída")
                api.excluir_categoria(cid)
                item["resumo"] = f"categoria criada {cid} excluída"
            else:
                item["resumo"] = "nada a desfazer"
            item["status"] = "ok"
            ok += 1
        except ErroNuvemshop as exc:
            item.update(status="falha", erro=str(exc)[:300])
            falhas += 1
        diario.registrar(item)
        log(f"  {item['acao']} #{acao['seq']}: {item['status']} — {item.get('resumo') or item.get('erro')}")
    diario.dados.update(status="concluida" if not falhas else "concluida_com_falhas", ok=ok, falhas=falhas,
                        concluida_em=datetime.now().isoformat(timespec="seconds"))
    diario.gravar()
    return destino


def conferir_reversao(api: NuvemshopAPI, aplicacao: Path, reversao: Path) -> list[dict]:
    """Compara, ao vivo, cada entidade tocada com o estado 'antes' registrado no diário."""
    ap = carregar_diario(Path(aplicacao))
    rv = json.loads(Path(reversao).read_text(encoding="utf-8"))
    mapa = {int(k): v for k, v in (rv.get("mapa_ids") or {}).items()}
    remap = lambda ids: sorted(mapa.get(i, i) for i in ids)  # noqa: E731
    checks = []
    for a in ap["acoes"]:
        if a.get("status") != "ok":
            continue
        tipo = a["acao"]
        if tipo in ("mover_produto", "arquivar_produto"):
            atual = api.obter_produto(a["produto"])
            esperado = remap(a["antes"]["categories"])
            ok = _ids(atual.get("categories")) == esperado
            if tipo == "arquivar_produto":
                ok = ok and _visibilidade(atual) == {k: v for k, v in a["antes"].items() if k in ("visibility", "published")}
            checks.append({"seq": a["seq"], "acao": tipo, "ok": ok, "atual": {**_visibilidade(atual),
                           "categories": _ids(atual.get("categories"))}, "esperado": esperado})
        elif tipo in ("renomear", "reparentar"):
            atual = api.obter_categoria(a["categoria"])
            pai = a["antes"]["parent"]
            ok = (atual.get("parent") or None) == mapa.get(pai, pai) and texto_i18n(atual.get("name")) == a["antes"]["nome"] \
                and texto_i18n(atual.get("handle")) == a["antes"]["handle"]
            checks.append({"seq": a["seq"], "acao": tipo, "ok": ok, "parent": atual.get("parent"),
                           "handle": texto_i18n(atual.get("handle"))})
        elif tipo == "excluir" and not a.get("ja_excluida"):
            novo = mapa.get(int(a["categoria"]))
            atual = api.obter_categoria(novo) if novo else {}
            ok = bool(novo) and texto_i18n(atual.get("name")) == texto_i18n(a["antes"].get("name")) \
                and (atual.get("parent") or None) == (a["antes"].get("parent") or None)
            checks.append({"seq": a["seq"], "acao": tipo, "ok": ok, "id_novo": novo,
                           "handle_antes": texto_i18n(a["antes"].get("handle")), "handle_novo": texto_i18n(atual.get("handle"))})
        elif tipo == "criar" and not a.get("reutilizada"):
            try:
                api.obter_categoria(a["categoria"])
                checks.append({"seq": a["seq"], "acao": tipo, "ok": False, "erro": "categoria criada ainda existe"})
            except ErroNuvemshop as exc:
                checks.append({"seq": a["seq"], "acao": tipo, "ok": exc.status == 404})
    return checks


def executar_piloto(*, log: Callable[[str], None] = print) -> dict[str, Any]:
    from wago.taxonomia import modelo
    from wago.taxonomia.inventario import gerar_snapshot, salvar_snapshot

    planos = sorted((modelo.TAXONOMIA / "planos").glob("plano-*.json"), key=lambda p: p.stat().st_mtime)
    if not planos:
        raise SystemExit("Nenhum plano encontrado. Rode `taxonomia plano` primeiro.")
    plano = json.loads(planos[-1].read_text(encoding="utf-8"))
    api = NuvemshopAPI.do_env()
    log("Snapshot completo da loja antes da escrita (referência do rollback)...")
    snapshot = gerar_snapshot(api, log=lambda m: None)
    caminho_snap = salvar_snapshot(snapshot)
    log(f"  snapshot: {caminho_snap.name} ({len(snapshot['categorias'])} categorias, {len(snapshot['produtos'])} produtos)")
    acoes = selecionar_piloto(plano, snapshot)
    log(f"Piloto: {len(acoes)} ação(ões) do plano {planos[-1].name}: "
        + ", ".join(f"#{a['seq']} {a['acao']}" for a in acoes))
    aplicacao = aplicar_acoes(api, acoes, plano_arquivo=planos[-1].name, snapshot_arquivo=caminho_snap.name, log=log)
    log(f"Diário da aplicação: {aplicacao}")
    return {"aplicacao": aplicacao, "snapshot": caminho_snap, "acoes": acoes}


def excluir_categorias_orfas(api: NuvemshopAPI, categorias: list[int], *, log: Callable[[str], None] = print,
                             destino: Path | None = None) -> Path:
    """Exclui categorias fora da taxonomia cujos produtos só as têm como ancestral órfão.

    Critério (autorizado pelo operador em 05/11/2026): todos os produtos vinculados já estão numa
    categoria final da taxonomia aprovada. O diário guarda as categorias de cada produto para o rollback.
    """
    from wago.taxonomia import modelo

    tax = modelo.carregar_aprovada()
    nos = {n["id_alvo"]: n for n in tax["nos"]}
    com_filhos = {n["pai"] for n in nos.values() if n.get("pai")}
    finais = {n["nuvemshop_id"] for i, n in nos.items() if n.get("nuvemshop_id") and i not in com_filhos}
    aprovadas = {n["nuvemshop_id"] for n in nos.values() if n.get("nuvemshop_id")}
    destino = destino or pasta_aplicacoes() / f"aplicacao-{datetime.now().strftime('%Y%m%dT%H%M%S')}-orfas.json"
    diario = Diario(destino, {"tipo": "aplicacao", "subtipo": "excluir_orfas", "store_id": api.store_id,
                              "iniciada_em": datetime.now().isoformat(timespec="seconds"), "status": "em_andamento"})
    ok = falhas = 0
    for seq, cid in enumerate(categorias, 1):
        item: dict[str, Any] = {"seq": seq, "acao": "excluir", "categoria": cid}
        try:
            if cid in aprovadas:
                raise ErroNuvemshop(409, "categoria pertence à taxonomia aprovada")
            antes = api.obter_categoria(cid)
            if antes.get("subcategories"):
                raise ErroNuvemshop(409, f"tem {len(antes['subcategories'])} subcategoria(s)")
            produtos = api.listar_produtos(campos="id,categories,published", category_id=cid)
            sem_final = [p["id"] for p in produtos if not (set(_ids(p.get("categories"))) & finais)]
            if sem_final:
                raise ErroNuvemshop(409, f"{len(sem_final)} produto(s) sem categoria final aprovada: {sem_final[:5]}")
            api.excluir_categoria(cid)
            try:
                api.obter_categoria(cid)
                raise ErroNuvemshop(409, "categoria ainda existe após o DELETE")
            except ErroNuvemshop as exc:
                if exc.status != 404:
                    raise
            item.update(status="ok", antes=antes, produtos_antes={str(p["id"]): _ids(p.get("categories")) for p in produtos},
                        resumo=f"'{texto_i18n(antes.get('name'))}' excluída ({len(produtos)} vínculo(s) órfão(s))")
            ok += 1
        except ErroNuvemshop as exc:
            item.update(status="falha", erro=str(exc)[:300])
            falhas += 1
        diario.registrar(item)
        log(f"  #{seq} excluir órfã {cid}: {item['status']} — {item.get('resumo') or item.get('erro')}")
    diario.dados.update(status="concluida" if not falhas else "concluida_com_falhas", ok=ok, falhas=falhas,
                        concluida_em=datetime.now().isoformat(timespec="seconds"))
    diario.gravar()
    return destino


def registrar_ids_criados(arquivo: Path) -> int:
    """Grava na taxonomia aprovada o `nuvemshop_id` das categorias criadas (evita recriar no próximo plano)."""
    from wago.taxonomia import modelo

    refs = carregar_diario(Path(arquivo)).get("refs") or {}
    caminho = modelo.arq_aprovada()
    tax = json.loads(caminho.read_text(encoding="utf-8"))
    n = 0
    for no in tax["nos"]:
        cid = refs.get(f"novo:{no['id_alvo']}")
        if cid and not no.get("nuvemshop_id"):
            no["nuvemshop_id"] = int(cid)
            n += 1
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(tax, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(caminho)
    return n


def _antes_do_snapshot(snapshot: dict) -> dict[int, dict]:
    return {p["id"]: {"categories": list(p.get("categorias") or []), "published": bool(p.get("publicado"))}
            for p in snapshot["produtos"]}


def executar_aplicacao_completa(*, log: Callable[[str], None] = print, retomar: Path | None = None,
                                cancelado: Callable[[], bool] | None = None) -> Path:
    """Aplica o plano mais recente inteiro. Progresso e falhas também vão para a fila/eventos do dashboard."""
    import uuid

    from wago.config import STATE
    from wago.dashboard.store import Store
    from wago.taxonomia import modelo
    from wago.taxonomia.inventario import gerar_snapshot, salvar_snapshot

    planos = sorted((modelo.TAXONOMIA / "planos").glob("plano-*.json"), key=lambda p: p.stat().st_mtime)
    if not planos:
        raise SystemExit("Nenhum plano encontrado. Rode `taxonomia plano` primeiro.")
    plano = json.loads(planos[-1].read_text(encoding="utf-8"))
    acoes = [a for fase in ORDEM for a in plano["fases"][fase]]

    store = Store(STATE / "dashboard.sqlite")
    ident = uuid.uuid4().hex[:12]
    store.criar_execucao(ident, "tax-aplicar", json.dumps({"plano": planos[-1].name, "acoes": len(acoes)}))
    etapa_id = store.iniciar_etapa(ident, "tax-aplicar")

    def log_dash(msg: str) -> None:
        log(msg)
        nivel = "erro" if ": falha" in msg else "aviso" if ": pulada" in msg else "info"
        store.evento(ident, "tax-aplicar", nivel, msg[:900])

    status, mensagem, registros = "falha", "", 0
    try:
        api = NuvemshopAPI.do_env(cancelado=cancelado)
        log_dash(f"Aplicação completa do plano {planos[-1].name}: {len(acoes)} ação(ões). Snapshot de segurança...")
        snapshot = gerar_snapshot(api, log=lambda m: None)
        caminho_snap = salvar_snapshot(snapshot)
        log_dash(f"Snapshot antes da escrita: {caminho_snap.name} ({len(snapshot['produtos'])} produtos)")
        destino = aplicar_acoes(api, acoes, plano_arquivo=planos[-1].name, snapshot_arquivo=caminho_snap.name,
                                log=log_dash, cancelado=cancelado, antes_produtos=_antes_do_snapshot(snapshot),
                                retomar=retomar, progresso_cada=100)
        diario = json.loads(destino.read_text(encoding="utf-8"))
        registros = diario.get("ok", 0)
        n_ids = registrar_ids_criados(destino)
        if n_ids:
            log_dash(f"{n_ids} categoria(s) criada(s) registradas na taxonomia (nuvemshop_id).")
        status = {"concluida": "concluida", "cancelada": "cancelada"}.get(diario["status"], "concluida_com_avisos")
        mensagem = (f"ok {diario.get('ok')} · falhas {diario.get('falhas')} · puladas {diario.get('puladas')}"
                    f" · diário {destino.name}")
        log_dash(f"Aplicação finalizada: {mensagem}")
        return destino
    except BaseException as exc:
        mensagem = f"{type(exc).__name__}: {exc}"[:500]
        store.evento(ident, "tax-aplicar", "erro", mensagem)
        raise
    finally:
        store.fechar_etapa(etapa_id, status=status, registros=registros, mensagem=mensagem)
        store.fechar_execucao(ident, status=status, registros=registros, mensagem=mensagem,
                              codigo=0 if status.startswith("concluida") else 1, arquivos="[]")
