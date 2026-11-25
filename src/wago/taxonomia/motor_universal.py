"""Motor Universal de Taxonomia: Diário Agnóstico, Aplicador Universal e Rollback Universal.

Este módulo implementa a aplicação agnóstica de planos de taxonomia v8,
suportando múltiplos providers (Nuvemshop, Bling) com tradução de IDs,
diário universal e reversão.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from wago.taxonomia.id_resolver import IDResolver
from wago.taxonomia.plano_validator import validar_plano
from wago.marketplaces.base import (
    BaseMarketplaceProvider,
    Categoria,
    NovaCategoria,
    AlteracaoCategoria,
    Produto,
    AlteracaoProduto,
    ErroMarketplace,
    NaoEncontrado,
    CategoriaComProdutos,
)

# Ordem de execução das fases (criar → renomear → reparentar → mover → arquivar → verificar → excluir)
ORDEM = ("criar", "renomear", "reparentar", "mover", "arquivar", "verificar", "excluir")
# Ordem de reversão (excluir ← reparentar ← renomear ← arquivar ← mover ← verificar ← criar)
ORDEM_REVERSAO = ("excluir", "reparentar", "renomear", "arquivar", "mover", "verificar", "criar")

# Mapeamento de ação para fase
ACAO_FASE = {
    "criar": "criar",
    "renomear": "renomear",
    "reparentar": "reparentar",
    "mover_produto": "mover",
    "arquivar_produto": "arquivar",
    "verificar_vazia": "verificar",
    "excluir": "excluir",
}


def carregar_diario_agnostico(arquivo: Path) -> dict:
    """Carrega diário agnóstico (compatível com diários antigos Nuvemshop).

    Diários antigos não têm campo 'provider' — assume 'nuvemshop'.
    """
    arquivo = Path(arquivo)
    dados = json.loads(arquivo.read_text(encoding="utf-8"))
    # Compatibilidade: diários antigos não têm 'provider'
    if "provider" not in dados:
        dados["provider"] = "nuvemshop"
    # Tenta carregar JSONL: primeiro pelo nome no cabeçalho, depois pelo mesmo stem do arquivo
    jsonl = None
    if dados.get("acoes_arquivo"):
        jsonl = arquivo.with_name(dados["acoes_arquivo"])
    if not jsonl or not jsonl.exists():
        # Fallback: mesmo stem do arquivo diário
        jsonl = arquivo.with_suffix(".acoes.jsonl")
    if jsonl and jsonl.exists():
        dados["acoes"] = [json.loads(linha) for linha in jsonl.read_text(encoding="utf-8").splitlines() if linha.strip()]
    dados.setdefault("acoes", [])
    return dados


class DiarioAgnostico:
    """Diário universal agnóstico de provider.

    Cabeçalho em `<nome>.json` + ações em `<nome>.acoes.jsonl` (append-only, à prova de queda).
    Inclui campo 'provider' para distinguir Nuvemshop/Bling.
    """

    def __init__(self, caminho: Path, cabecalho: dict) -> None:
        self.caminho = caminho
        self.jsonl = caminho.with_suffix(".acoes.jsonl")
        # Garante campos obrigatórios no cabeçalho
        cabecalho_completo = {
            **cabecalho,
            "refs": cabecalho.get("refs", {}),
            "acoes_arquivo": self.jsonl.name,
        }
        self.dados = cabecalho_completo
        self.acoes: list[dict] = []
        caminho.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl.write_text("", encoding="utf-8")
        self.gravar()

    def registrar(self, item: dict) -> None:
        """Registra uma ação no diário (memória + JSONL)."""
        self.acoes.append(item)
        with self.jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        # Grava cabeçalho a cada 25 ações ou em ações 'criar' (para capturar refs)
        if len(self.acoes) % 25 == 0 or item.get("acao") == "criar":
            self.gravar()

    def gravar(self) -> None:
        """Grava cabeçalho atomicamente (.tmp + os.replace)."""
        tmp = self.caminho.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({**self.dados, "registradas": len(self.acoes)}, ensure_ascii=False, indent=1),
            encoding="utf-8"
        )
        tmp.replace(self.caminho)


class AplicadorUniversal:
    """Aplicador universal de plano de taxonomia para qualquer provider."""

    def __init__(
        self,
        provider: BaseMarketplaceProvider,
        resolver: IDResolver,
        diario: DiarioAgnostico,
        snapshot_antes: dict[int, dict],
        log: Callable[[str], None] = print,
        progresso_cada: int = 0,
        tamanho_lote: int = 50,
        pausa_lote: float = 30,
        cancelado: Callable[[], bool] | None = None,
    ) -> None:
        self.tamanho_lote = tamanho_lote
        self.pausa_lote = pausa_lote
        self.cancelado = cancelado
        self.provider = provider
        self.resolver = resolver
        self.diario = diario
        self.snapshot_antes = snapshot_antes
        self.log = log
        self.progresso_cada = progresso_cada

    def executar(self, acoes: list[dict]) -> dict[str, int]:
        """Executa lista de ações do plano, registrando tudo no diário.

        Returns:
            Dict com contadores: ok, falhas, puladas.
        """
        # 1. Valida plano (já deve ter sido validado antes, mas garante)
        validar_plano({"fases": {f: [] for f in ORDEM}}, self.provider.nome)
        if self.provider.nome == "bling":
            from wago.taxonomia.aplicar import aplicar_acoes_universal

            aplicar_acoes_universal(
                self.provider, acoes, plano_arquivo=self.diario.dados.get("plano", self.diario.caminho.name),
                snapshot_arquivo=self.diario.dados.get("snapshot_antes", ""), diario=self.diario,
                antes_produtos=self.snapshot_antes, resolver=self.resolver, log=self.log,
                progresso_cada=self.progresso_cada, tamanho_lote=self.tamanho_lote,
                pausa_lote=self.pausa_lote, cancelado=self.cancelado,
            )
            return {k: self.diario.dados.get(k, 0) for k in ("ok", "falhas", "puladas")}

        # 2. Ordena ações por fase (respeitando ORDEM)
        ordenadas = self._ordenar_acoes(acoes)

        # 3. Executa cada ação
        ok = falhas = puladas = 0
        por_fase: Counter = Counter()
        inicio = time.monotonic()
        verificacoes_falhas: set[int] = set()

        # Para excluir: verifica se verificar_vazia falhou
        seq_excluir = {a.get("categoria"): a["seq"] for a in acoes if a.get("acao") == "excluir"}

        def chave_ordenacao(a: dict) -> tuple:
            fase = ACAO_FASE.get(a["acao"], "criar")
            if a["acao"] == "verificar_vazia" and a.get("categoria") in seq_excluir:
                return (ORDEM.index("excluir"), seq_excluir[a["categoria"]], 0)
            return (ORDEM.index(fase), a.get("seq", 0), 1)

        ordenadas.sort(key=chave_ordenacao)

        for n, acao in enumerate(ordenadas, 1):
            # Sincroniza refs do resolver com o diário antes de resolver a ação
            self.resolver.diario_refs = self.diario.dados["refs"]

            item: dict[str, Any] = {"seq": acao["seq"], "acao": acao["acao"], "em": datetime.now().isoformat(timespec="seconds")}

            # Pula exclusão se verificação falhou
            if acao["acao"] == "excluir" and acao.get("categoria") in verificacoes_falhas:
                item.update(status="pulada", erro="verificação de categoria vazia falhou: exclusão não executada")
                puladas += 1
            else:
                try:
                    resultado = self._executar_acao(acao)
                    item.update(resultado)
                    item["status"] = "ok"
                    ok += 1
                    # Captura refs de ações 'criar'
                    if acao["acao"] == "criar" and "categoria" in resultado:
                        ref = acao.get("ref")
                        if ref and ref.startswith("novo:"):
                            self.diario.dados["refs"][ref] = resultado["categoria"]
                except Exception as exc:  # noqa: BLE001 — uma ação nunca derruba o lote
                    item.update(status="falha", erro=f"{type(exc).__name__}: {exc}"[:300])
                    falhas += 1
                    if acao["acao"] == "verificar_vazia":
                        verificacoes_falhas.add(acao["categoria"])

            por_fase[f"{ACAO_FASE[acao['acao']]}:{item['status']}"] += 1
            self.diario.registrar(item)

            # Log
            if item["status"] != "ok" or not self.progresso_cada:
                resumo = item.get("resumo")
                erro = item.get("erro")
                self.log(f"  #{acao['seq']} {acao['acao']}: {item['status']}"
                         + (f" — {resumo}" if resumo and item["status"] == "ok" else "")
                         + (f" — {erro}" if erro else ""))

            # Progresso periódico
            if self.progresso_cada and (n % self.progresso_cada == 0 or n == len(ordenadas)):
                decorrido = time.monotonic() - inicio
                ritmo = n / max(decorrido, 1)
                restante = (len(ordenadas) - n) / max(ritmo, 0.01)
                self.log(f"Progresso: {n}/{len(ordenadas)} ({100 * n / len(ordenadas):.1f}%) · ok {ok} · falhas {falhas}"
                         f" · puladas {puladas} · fase {ACAO_FASE[acao['acao']]} · ~{restante / 60:.0f} min restantes")
                self.diario.dados["progresso"] = {"feitas": n, "ok": ok, "falhas": falhas, "puladas": puladas}
                self.diario.gravar()

        # Finaliza
        self.diario.dados.update(
            status="concluida" if not falhas and not puladas else "concluida_com_falhas",
            concluida_em=datetime.now().isoformat(timespec="seconds"),
            ok=ok, falhas=falhas, puladas=puladas, por_fase=dict(por_fase)
        )
        self.diario.gravar()

        return {"ok": ok, "falhas": falhas, "puladas": puladas}

    def _ordenar_acoes(self, acoes: list[dict]) -> list[dict]:
        """Ordena ações pela fase (ORDEM) e seq."""
        def chave(a: dict) -> tuple:
            fase = ACAO_FASE.get(a["acao"], "criar")
            return (ORDEM.index(fase), a.get("seq", 0))
        return sorted(acoes, key=chave)

    def _executar_acao(self, acao: dict) -> dict:
        """Executa uma única ação delegando ao provider."""
        tipo = acao["acao"]

        # Traduz IDs usando resolver
        acao_traduzida = self.resolver.resolver_acao(acao)

        if tipo == "criar":
            return self._criar_categoria(acao_traduzida)
        elif tipo == "renomear":
            return self._renomear_categoria(acao_traduzida)
        elif tipo == "reparentar":
            return self._reparentar_categoria(acao_traduzida)
        elif tipo == "mover_produto":
            return self._mover_produto(acao_traduzida)
        elif tipo == "arquivar_produto":
            return self._arquivar_produto(acao_traduzida)
        elif tipo == "verificar_vazia":
            return self._verificar_vazia(acao_traduzida)
        elif tipo == "excluir":
            return self._excluir_categoria(acao_traduzida)
        else:
            raise ValueError(f"Ação desconhecida: {tipo}")

    def _criar_categoria(self, acao: dict) -> dict:
        """Cria categoria via provider."""
        dados: NovaCategoria = {
            "nome": acao["nome"],
            "pai_id": acao.get("pai"),
            "descricao": acao.get("descricao", ""),
        }
        cat = self.provider.criar_categoria(dados)
        return {
            "categoria": cat["id"],
            "ref": acao.get("ref"),
            "reutilizada": cat.get("reutilizada", False),
            "depois": {"nome": cat["nome"], "pai": cat.get("pai_id"), "descricao": cat.get("descricao", "")},
            "resumo": f"'{acao['nome']}' → id {cat['id']}{' (reutilizada)' if cat.get('reutilizada') else ''}"
        }

    def _obter_categoria(self, cid: int) -> dict:
        """Busca categoria por ID na lista do provider."""
        categorias = self.provider.listar_categorias()
        return next((c for c in categorias if c["id"] == cid), {})

    def _obter_produto(self, pid: int) -> dict:
        """Busca produto por ID na lista do provider."""
        produtos = self.provider.listar_produtos()
        return next((p for p in produtos if p["id"] == pid), {})

    def _renomear_categoria(self, acao: dict) -> dict:
        """Renomeia categoria via provider."""
        cid = acao["categoria"]
        antes_cat = self._obter_categoria(cid)
        # Copia os dados antes da alteração (evita mutação por referência)
        antes = {"nome": antes_cat.get("nome"), "pai_id": antes_cat.get("pai_id")}
        self.provider.atualizar_categoria(cid, {"nome": acao["nome"]})
        depois_cat = self._obter_categoria(cid)
        depois = {"nome": depois_cat.get("nome"), "pai_id": depois_cat.get("pai_id")}
        return {
            "categoria": cid,
            "antes": antes,
            "depois": depois,
            "resumo": f"{antes['nome']} → {depois['nome']}"
        }

    def _reparentar_categoria(self, acao: dict) -> dict:
        """Reparenta categoria via provider (só Nuvemshop; Bling bloqueado no validador)."""
        cid = acao["categoria"]
        antes_cat = self._obter_categoria(cid)
        antes = {"nome": antes_cat.get("nome"), "pai_id": antes_cat.get("pai_id")}
        self.provider.atualizar_categoria(cid, {"pai_id": acao["para"]})
        depois_cat = self._obter_categoria(cid)
        depois = {"nome": depois_cat.get("nome"), "pai_id": depois_cat.get("pai_id")}
        return {
            "categoria": cid,
            "antes": antes,
            "depois": depois,
            "resumo": f"pai {antes['pai_id']} → {depois['pai_id']}"
        }

    def _mover_produto(self, acao: dict) -> dict:
        """Move produto entre categorias via provider."""
        pid = acao["produto"]
        # snapshot_antes usa chaves "categorias" e "publicado" (conforme fixture de teste)
        antes_cats = list(self.snapshot_antes.get(pid, {}).get("categorias", []))
        self.provider.atualizar_produto(pid, {"categorias": acao["para"]})
        depois = self._obter_produto(pid)
        return {
            "produto": pid,
            "antes": {"categorias": antes_cats},
            "depois": {"categorias": list(depois.get("categorias", []))},
            "resumo": f"{antes_cats} → {depois.get('categorias', [])}"
        }

    def _arquivar_produto(self, acao: dict) -> dict:
        """Arquiva produto (publicado=False) via provider."""
        pid = acao["produto"]
        antes_cats = list(self.snapshot_antes.get(pid, {}).get("categorias", []))
        antes_pub = self.snapshot_antes.get(pid, {}).get("publicado", True)
        payload: AlteracaoProduto = {"publicado": False}
        if "categorias" in acao:
            payload["categorias"] = acao["categorias"]
        self.provider.atualizar_produto(pid, payload)
        depois = self._obter_produto(pid)
        return {
            "produto": pid,
            "antes": {"categorias": antes_cats, "publicado": antes_pub},
            "depois": {"categorias": list(depois.get("categorias", [])), "publicado": depois.get("publicado")},
            "resumo": f"arquivado: {antes_cats} → {depois.get('categorias', [])}, publicado {antes_pub} → {depois.get('publicado')}"
        }

    def _verificar_vazia(self, acao: dict) -> dict:
        """Verifica se categoria está vazia (sem produtos e sem subcategorias).

        Para Bling (e outros providers sem listar_produtos_da_categoria),
        usa listar_produtos() + filtra por categoria.
        """
        cid = acao["categoria"]

        # Busca categoria para ver subcategorias
        categorias = self.provider.listar_categorias()
        cat = next((c for c in categorias if c["id"] == cid), None)
        if cat is None:
            return {"categoria": cid, "produtos": 0, "subcategorias": 0, "ja_ausente": True,
                    "resumo": "categoria já não existe"}

        # Conta produtos na categoria (usa listar_produtos + filtro)
        produtos = self.provider.listar_produtos()
        produtos_na_cat = [p for p in produtos if cid in p.get("categorias", [])]

        # Conta subcategorias
        subcats = [c for c in categorias if c.get("pai_id") == cid]

        if produtos_na_cat or subcats:
            raise ErroMarketplace(409, f"categoria não está vazia: {len(produtos_na_cat)} produto(s), {len(subcats)} subcategoria(s)")

        return {"categoria": cid, "produtos": len(produtos_na_cat), "subcategorias": len(subcats), "resumo": "vazia confirmada"}

    def _excluir_categoria(self, acao: dict) -> dict:
        """Exclui categoria via provider."""
        cid = acao["categoria"]
        antes_cat = self._obter_categoria(cid)
        if not antes_cat:
            return {"categoria": cid, "ja_excluida": True, "resumo": "já não existia (404)"}

        # Copia os dados antes da exclusão
        antes = {"nome": antes_cat.get("nome"), "pai_id": antes_cat.get("pai_id"), "descricao": antes_cat.get("descricao", "")}
        self.provider.deletar_categoria(cid)
        return {"categoria": cid, "antes": antes, "resumo": f"'{antes.get('nome', cid)}' excluída"}


def reverter_universal(
    arquivo_diario: Path,
    provider: BaseMarketplaceProvider,
    *,
    log: Callable[[str], None] = print
) -> Path:
    """Desfaz as ações 'ok' de um diário universal.

    Executa na ordem reversa: excluir → reparentar → renomear → arquivar → mover → verificar → criar.
    """
    dados = carregar_diario_agnostico(arquivo_diario)

    # Verifica compatibilidade de provider
    if dados.get("provider") != provider.nome:
        log(f"AVISO: Diário foi gerado para provider '{dados.get('provider')}', mas rollback roda em '{provider.nome}'")

    destino = Path(arquivo_diario).with_name(
        Path(arquivo_diario).stem.replace("aplicacao", "reversao") + ".json"
    )
    diario_rev = DiarioAgnostico(destino, {
        "tipo": "reversao", "de": Path(arquivo_diario).name,
        "provider": provider.nome, "store_id": dados.get("store_id"),
        "iniciada_em": datetime.now().isoformat(timespec="seconds"), "status": "em_andamento"
    })

    # Mapa de IDs recriados (para excluir → criar)
    mapa_ids: dict[int, int] = {}
    diario_rev.dados["mapa_ids"] = mapa_ids

    def remap(ids: list[int]) -> list[int]:
        return [mapa_ids.get(i, i) for i in ids]

    def obter_categoria(cid: int) -> dict:
        categorias = provider.listar_categorias()
        return next((c for c in categorias if c["id"] == cid), {})

    def obter_produto(pid: int) -> dict:
        produtos = provider.listar_produtos()
        return next((p for p in produtos if p["id"] == pid), {})

    ok = falhas = 0
    # Ações com status 'ok', ordenadas reversamente
    aplicadas = [a for a in dados["acoes"] if a.get("status") == "ok"]
    aplicadas.sort(key=lambda a: (ORDEM_REVERSAO.index(ACAO_FASE.get(a["acao"], "criar")), -a["seq"]))

    for acao in aplicadas:
        item: dict[str, Any] = {"seq": acao["seq"], "acao": f"reverter_{acao['acao']}"}
        try:
            tipo = acao["acao"]

            if tipo == "excluir" and not acao.get("ja_excluida"):
                # Recria categoria excluída
                antes = acao["antes"]
                extra = {}
                for k in ("descricao",):
                    v = antes.get(k)
                    if v:
                        extra[k] = v
                pai = antes.get("pai_id") or antes.get("pai")
                cat = provider.criar_categoria({
                    "nome": antes["nome"],
                    "pai_id": mapa_ids.get(pai, pai) if pai else None,
                    **extra
                })
                mapa_ids[acao["categoria"]] = cat["id"]
                item["resumo"] = f"recriada '{antes['nome']}' id {acao['categoria']} → {cat['id']}"

                # Religar produtos que estavam nela
                produtos_antes = acao.get("produtos_antes", {})
                for pid, cats in produtos_antes.items():
                    provider.atualizar_produto(int(pid), {"categorias": remap(cats)})
                if produtos_antes:
                    item["resumo"] += f" · {len(produtos_antes)} produto(s) religados"

            elif tipo == "arquivar_produto":
                # Restaura produto arquivado
                pid = acao["produto"]
                antes = acao["antes"]
                # snapshot usa "categorias" e "publicado"
                payload: AlteracaoProduto = {
                    "categorias": remap(antes.get("categorias", antes.get("categories", []))),
                    "publicado": antes.get("publicado", antes.get("published", True))
                }
                provider.atualizar_produto(pid, payload)
                depois = obter_produto(pid)
                item["resumo"] = f"restaurado publicado={depois.get('publicado')} categorias={depois.get('categorias')}"

            elif tipo == "mover_produto":
                # Restaura categorias anteriores do produto
                pid = acao["produto"]
                antes = acao["antes"]
                provider.atualizar_produto(pid, {"categorias": remap(antes.get("categorias", antes.get("categories", [])))})
                depois = obter_produto(pid)
                item["resumo"] = f"categorias restauradas {depois.get('categorias')}"

            elif tipo == "reparentar":
                # Restaura pai anterior
                cid = acao["categoria"]
                pai_antes = acao["antes"].get("pai") or acao["antes"].get("pai_id")
                if provider.nome == "bling":
                    # Bling não suporta reparentar via API
                    raise ErroMarketplace(400, "Bling não suporta reparentar categorias pela API v3 (irreversível)")
                provider.atualizar_categoria(cid, {"pai_id": mapa_ids.get(pai_antes, pai_antes) if pai_antes else None})
                depois = obter_categoria(cid)
                item["resumo"] = f"pai restaurado → {depois.get('pai_id')}"

            elif tipo == "renomear":
                # Restaura nome anterior
                cid = acao["categoria"]
                provider.atualizar_categoria(cid, {"nome": acao["antes"]["nome"]})
                item["resumo"] = f"nome restaurado → {acao['antes']['nome']}"

            elif tipo == "criar" and not acao.get("reutilizada"):
                # Exclui categoria criada (se não foi reutilizada)
                cid = acao["categoria"]
                # Verifica se tem produtos/subcategorias
                try:
                    produtos = provider.listar_produtos()
                    if any(cid in p.get("categorias", []) for p in produtos):
                        raise ErroMarketplace(409, "categoria criada ainda tem produtos; não excluída")
                    categorias = provider.listar_categorias()
                    if any(c.get("pai_id") == cid for c in categorias):
                        raise ErroMarketplace(409, "categoria criada ainda tem subcategorias; não excluída")
                except NaoEncontrado:
                    pass  # já não existe
                provider.deletar_categoria(cid)
                item["resumo"] = f"categoria criada {cid} excluída"

            else:
                item["resumo"] = "nada a desfazer"

            item["status"] = "ok"
            ok += 1

        except ErroMarketplace as exc:
            item.update(status="falha", erro=str(exc)[:300])
            falhas += 1
        except Exception as exc:  # noqa: BLE001
            item.update(status="falha", erro=f"{type(exc).__name__}: {exc}"[:300])
            falhas += 1

        diario_rev.registrar(item)
        log(f"  {item['acao']} #{acao['seq']}: {item['status']} — {item.get('resumo') or item.get('erro')}")

    diario_rev.dados.update(
        status="concluida" if not falhas else "concluida_com_falhas",
        ok=ok, falhas=falhas,
        concluida_em=datetime.now().isoformat(timespec="seconds")
    )
    diario_rev.gravar()
    return destino