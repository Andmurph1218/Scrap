"""Fase 2 — Taxonomia-alvo: geração da proposta, validação, edição e gate de aprovação."""

from __future__ import annotations

import csv
import glob
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from wago.config import (
    OUT,
    TAXONOMIA,
    TAXONOMIA_LIMITE_CONSOLIDACAO,
    TAXONOMIA_MAX_NIVEIS,
    ensure_dirs,
)
from wago.taxonomia.inventario import motivos_nome_suspeito
from wago.taxonomia.texto import normalizar_nome, sem_acentos

STATUS_PENDENTE = "pendente_aprovacao"
STATUS_APROVADA = "aprovada"
_STOPWORDS = {"de", "da", "do", "das", "dos", "e", "para", "com", "em", "a", "o", "p"}
_MOTIVOS_DESCARTE = {"fragmento de breadcrumb", "contém código/SKU", "nome vazio"}

# ---------------------------------------------------------------- regras do cliente
MAX_CHARS_CATEGORIA = 40
LIMITE_CONSOLIDACAO = TAXONOMIA_LIMITE_CONSOLIDACAO

# Tradução/sinônimos das categorias da Wago que chegam em inglês (chave: nome original).
TRADUCOES_WAGO: dict[str, str] = {
    "Interface Modules and System Wiring": "Módulos de Interface e Cabeamento",
    "Energy Measurement Technology": "Medição de Energia",
    "Component Modules": "Módulos de Componentes",
}

# Resumos de nomes que excedem MAX_CHARS_CATEGORIA (nome completo ou trecho → forma curta).
ABREVIACOES: dict[str, str] = {
    "Bornes para placas de circuito impresso e conectores plugáveis": "Bornes para PCI e Conectores",
    "Transdutores de sinal e amplificadores de isolamento": "Transdutores e Amplificadores de Sinal",
    "Dispositivo De Proteção Contra Surto(DPS)": "DPS - Proteção Contra Surto",
    "placas de circuito impresso": "PCI",
}

# Categorias genéricas: nunca recebem produto (só podem existir como pai com subcategorias).
CATEGORIAS_GENERICAS = {"Outros", "Acessórios", "Diversos", "Geral"}
_GENERICAS_NORM = {normalizar_nome(n) for n in CATEGORIAS_GENERICAS}

# Consolidação de árvores duplicadas: a sobrevivente é a que tiver mais subcategorias.
CONSOLIDACOES_ARVORE: list[tuple[str, ...]] = [
    ("Materiais Elétricos", "Comutação e Potência"),
]

# Taxonomia v3: estas 7 raízes são obrigatórias. O restante da loja entra debaixo delas.
MACRO_RAIZES: tuple[str, ...] = (
    "Materiais Elétricos",
    "Automação e Controle",
    "Iluminação",
    "Ferramentas",
    "Energia Solar",
    "EPI e Segurança",
    "Fixação e Utilidades",
)
# Nome atual da loja → macro que o absorve. A comparação usa normalizar_nome.
_DESTINO_POR_NOME: dict[str, tuple[str, ...]] = {
    "Materiais Elétricos": (
        "Materiais Elétricos", "Bornes e conectores de instalação", "Bornes montados em trilho",
        "Bornes para fiação de campo", "Bornes para PCI e Conectores", "Componentes de passagem",
        "Conectores plugáveis", "Infraestrutura de Painel", "Interruptores", "Marcação", "Rotulagem",
        "Módulos de alimentação de energia", "Módulos de Componentes", "Módulos de Interface e Cabeamento",
        "Módulos de relés e acopladores óticos", "Plugs e Tomadas Industriais", "Plugs e Tomadas Residenciais",
        "Proteção contra Surtos (DPS)", "Proteção contra sobretensão", "Medição de Energia", "Placas",
    ),
    "Automação e Controle": (
        "Automação e Controle", "Automação Industrial", "Coleta de Dados", "Comando e Sinalização",
        "Controle e Lógica", "Controlador", "Edge Devices", "Painel touch", "Sensores",
        "Sensores e Detecção", "Caixas de sensores/atuadores", "Sistemas de I/O", "Software",
        "Switches Industriais", "Transdutores e Amplificadores de Sinal", "Pneumática",
        "Building Ecosystem",
    ),
    "Iluminação": ("Iluminação", "Conexão de iluminação"),
    "Ferramentas": ("Ferramentas",),
    "Energia Solar": ("Energia Solar", "Solar"),
    "EPI e Segurança": ("EPI e Segurança", "Bloqueio e Travamento"),
    "Fixação e Utilidades": (
        "Fixação e Utilidades", "Elementos de Fixação", "Utilidades e Manutenção",
    ),
}
_MACRO_POR_NOME = {
    normalizar_nome(nome): macro
    for macro, nomes in _DESTINO_POR_NOME.items()
    for nome in nomes
}
# Alias adotado como a própria macro, em vez de virar um nível extra (Solar → Energia Solar).
_ALIAS_DA_MACRO = {"Energia Solar": {normalizar_nome("Solar")}}
_FALLBACK_MACRO: tuple[tuple[tuple[str, ...], str], ...] = (
    (("solar", "fotovolta"), "Energia Solar"),
    (("ilumina", "lampada", "luminaria"), "Iluminação"),
    (("ferramenta",), "Ferramentas"),
    (("epi", "seguranca"), "EPI e Segurança"),
    (("fixacao", "parafuso", "abracadeira", "utilidade"), "Fixação e Utilidades"),
    (("sensor", "automacao", "controle", "clp"), "Automação e Controle"),
    (("borne", "cabo", "disjuntor", "rele", "contator", "tomada", "interruptor"), "Materiais Elétricos"),
)
_FOLHAS_FIXACAO = ("Parafusos", "Abraçadeiras", "Manutenção")

# Tradução dos nós que a loja ainda guarda em inglês.
TRADUCOES_V3: tuple[tuple[str, str], ...] = (
    ("Painel touch", "Painéis Touch"),
    ("Edge Devices", "Dispositivos de Borda"),
    ("Building Ecosystem", "Ecossistema Predial"),
)

# Lista reativada da v5, mais os termos de Utilidades e Manutenção.
TERMOS_FORA_DO_RAMO: tuple[str, ...] = (
    "Arame Farpado", "Calculadora", "Calculadoras", "Caneta Esferográfica", "Espremedor",
    "Linha de Costura", "Linhas de Costura", "Material de Escritório", "Papel Sulfite",
    "Papelaria", "Potes", "Tecidos", "Utensílios Domésticos", "Utensílios de Cozinha",
    "Utilidades Domésticas", "Escolar", "Lixeira", "Limpeza",
)

# Folhas de Utilidades e Manutenção que saem do ramo inteiras.
FOLHAS_FORA_UTILIDADES: tuple[str, ...] = (
    "Material Escolar e Escritório", "Lixeiras", "Utensílios de Limpeza",
    "Canetas e Marcadores", "Arquivos para Escritório",
)

_TRADUCOES_NORM = {normalizar_nome(k): v for k, v in {**TRADUCOES_WAGO, **ABREVIACOES}.items()}


def e_generica(nome: str) -> bool:
    return normalizar_nome(nome) in _GENERICAS_NORM


def ajustar_nome(nome: str) -> str:
    """Traduz (dicionário Wago) e resume nomes acima de MAX_CHARS_CATEGORIA."""
    nome = re.sub(r"\s+", " ", str(nome or "")).strip()
    nome = _TRADUCOES_NORM.get(normalizar_nome(nome), nome)
    if len(nome) > MAX_CHARS_CATEGORIA:
        for longo, curto in ABREVIACOES.items():
            nome = re.sub(re.escape(longo), curto, nome, flags=re.IGNORECASE)
    return nome


def arq_proposta() -> Path:
    return TAXONOMIA / "taxonomia.proposta.json"


def arq_aprovada() -> Path:
    return TAXONOMIA / "taxonomia.json"


def pasta_historico() -> Path:
    return TAXONOMIA / "historico"


# ---------------------------------------------------------------- utilidades
def slug(nome: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", sem_acentos(nome).lower()).strip("-")
    return base or "categoria"


def palavras_chave(nome: str) -> list[str]:
    return sorted({p for p in normalizar_nome(nome).split() if p not in _STOPWORDS and len(p) > 1})


def _novo_id(nome: str, usados: set[str]) -> str:
    base, n = slug(nome), 2
    ident = base
    while ident in usados:
        ident, n = f"{base}-{n}", n + 1
    usados.add(ident)
    return ident


def _no(id_alvo: str, nome: str, pai: str | None, **extra: Any) -> dict:
    return {
        "id_alvo": id_alvo, "nome": nome, "pai": pai,
        "sinonimos": [], "palavras_chave": palavras_chave(nome),
        "nuvemshop_id": None, "nuvemshop_ids_mesclados": [],
        "origem": [], "produtos_estimados": 0, "alertas": [], **extra,
    }


def cadeia_ate_raiz(por_id: dict[str, dict], ident: str) -> list[str]:
    """Do nó até a raiz, o próprio nó primeiro. Para ao encontrar um ciclo."""
    cadeia: list[str] = []
    vistos: set[str] = set()
    atual: str | None = ident
    while atual and atual in por_id and atual not in vistos:
        vistos.add(atual)
        cadeia.append(atual)
        atual = por_id[atual].get("pai")
    return cadeia


def rotulo_caminho(por_id: dict[str, dict], ident: str, sep: str = " › ") -> str:
    """Rótulo de tela, da raiz até o nó. Ex.: Materiais Elétricos › Instalação › Bornes."""
    ids = list(reversed(cadeia_ate_raiz(por_id, ident)))
    return sep.join(por_id[i]["nome"] for i in ids if i in por_id)


def caminho_estavel(por_id: dict[str, dict], ident: str) -> str:
    """Chave estável da folha, da raiz até o nó, nomes normalizados.

    Ex.: "material eletrico > instalacao > borne". Usa o mesmo normalizador do
    motor (minúsculas, sem acento, singular), então "Bornes" e "Borne" são a mesma folha.
    Sobrevive a uma troca de id_alvo.
    """
    ids = list(reversed(cadeia_ate_raiz(por_id, ident)))
    return " > ".join(normalizar_nome(por_id[i]["nome"]) for i in ids if i in por_id)


def _erros_de_profundidade(por_id: dict[str, dict]) -> list[str]:
    """Caminha cada nó até a raiz. Raiz é o nível 1. Acima de TAXONOMIA_MAX_NIVEIS é erro.

    Um ciclo (A → B → A) é reportado uma vez e não conta como profundidade.
    """
    prof: dict[str, int] = {}
    erros: list[str] = []
    ja_reportado: set[str] = set()

    def caminhar(ident: str, pilha: tuple[str, ...]) -> int | None:
        if ident in prof:
            return prof[ident]
        if ident in pilha:
            if ident not in ja_reportado:
                volta = list(pilha[pilha.index(ident):]) + [ident]
                nomes = " → ".join(por_id[i]["nome"] for i in volta if i in por_id)
                erros.append(f"ciclo na árvore: {nomes}")
                ja_reportado.update(volta)
            return None
        no = por_id.get(ident)
        if no is None:
            return None
        pai = no.get("pai")
        if not pai or pai not in por_id:
            prof[ident] = 1
            return 1
        base = caminhar(pai, pilha + (ident,))
        if base is None:
            return None
        prof[ident] = base + 1
        return prof[ident]

    for ident in por_id:
        caminhar(ident, ())
    for ident, nivel in prof.items():
        if nivel > TAXONOMIA_MAX_NIVEIS:
            nome = por_id[ident]["nome"]
            erros.append(f"{nome}: excede {TAXONOMIA_MAX_NIVEIS} níveis (está no nível {nivel})")
    return erros


# ---------------------------------------------------------------- validação
def validar_taxonomia(tax: dict) -> list[str]:
    erros: list[str] = []
    nos = tax.get("nos") or []
    ids = [n.get("id_alvo") for n in nos]
    por_id = {n["id_alvo"]: n for n in nos if n.get("id_alvo")}
    for ident, qtd in Counter(ids).items():
        if not ident:
            erros.append("nó sem id_alvo")
        elif qtd > 1:
            erros.append(f"id_alvo duplicado: {ident}")
    nomes_por_pai: dict[str | None, Counter] = defaultdict(Counter)
    tem_filhos = {n.get("pai") for n in nos if n.get("pai")}
    for n in nos:
        if not str(n.get("nome") or "").strip():
            erros.append(f"nó {n.get('id_alvo')} sem nome")
            continue
        if len(n["nome"]) > MAX_CHARS_CATEGORIA:
            erros.append(
                f"{n['nome']}: nome com {len(n['nome'])} caracteres (máx. {MAX_CHARS_CATEGORIA}); resuma"
            )
        if e_generica(n["nome"]) and n["id_alvo"] not in tem_filhos:
            erros.append(
                f"{n['nome']}: categoria genérica não pode receber produtos; "
                "reclassifique/mescle ou use-a apenas como pai de subcategorias"
            )
        pai = n.get("pai")
        if pai is not None:
            if pai not in por_id:
                erros.append(f"{n['nome']}: pai inexistente ({pai})")
            elif pai == n["id_alvo"]:
                erros.append(f"{n['nome']}: é pai de si mesmo")
        nomes_por_pai[pai][normalizar_nome(n["nome"])] += 1
    for pai, contagem in nomes_por_pai.items():
        for nome, qtd in contagem.items():
            if qtd > 1:
                erros.append(f"nome repetido no mesmo nível ({pai or 'raiz'}): {nome}")
    erros.extend(_erros_de_profundidade(por_id))
    return erros


# ---------------------------------------------------------------- fontes
def categorias_wago(pasta: Path = OUT) -> Counter:
    """Categoria principal em português de cada SKU nos masters (contagem por SKU único)."""
    por_sku: dict[str, str] = {}
    for caminho in sorted(glob.glob(str(pasta / "*master.csv"))):
        try:
            with open(caminho, encoding="utf-8-sig", newline="") as fh:
                for linha in csv.DictReader(fh, delimiter=";"):
                    sku = (linha.get("SKU") or "").strip().upper()
                    cat = (linha.get("Categorias") or "").split(">")[0].split(",")[0].strip()
                    if sku and cat:
                        por_sku[sku] = cat
        except (OSError, csv.Error):
            continue
    return Counter(por_sku.values())


def _destino_macro(nome: str) -> str | None:
    chave = normalizar_nome(nome)
    if chave in _MACRO_POR_NOME:
        return _MACRO_POR_NOME[chave]
    for termos, macro in _FALLBACK_MACRO:
        if any(termo in chave for termo in termos):
            return macro
    return None


def _adotar_macro(nos: list[dict], nome: str, aliases: set[str], usados: set[str]) -> dict:
    """Acha a raiz com esse nome, renomeia um alias (Solar → Energia Solar) ou cria a raiz."""
    chave = normalizar_nome(nome)
    exato = next((n for n in nos if not n.get("pai") and normalizar_nome(n["nome"]) == chave), None)
    if exato:
        return exato
    alias = next((n for n in nos if not n.get("pai") and normalizar_nome(n["nome"]) in aliases), None)
    if alias:
        if alias["nome"] != nome:
            if alias["nome"] not in alias["sinonimos"]:
                alias["sinonimos"].append(alias["nome"])
            alias["nome_atual_loja"] = alias.get("nome_atual_loja") or alias["nome"]
            alias["nome"] = nome
        return alias
    no = _no(_novo_id(nome, usados), nome, None)
    no["origem"] = ["v3"]
    nos.append(no)
    return no


def _altura(nos: list[dict], ident: str) -> int:
    filhos = [n["id_alvo"] for n in nos if n.get("pai") == ident]
    if not filhos:
        return 1
    return 1 + max(_altura(nos, filho) for filho in filhos)


def _realocar_outros(tax: dict, fixacao: dict, usados: set[str]) -> None:
    """Elimina Outros. Filhas e produtos passam para Fixação e Utilidades."""
    nos: list[dict] = tax["nos"]
    mapa = tax.setdefault("mapa_categorias_loja", {})
    motivo = "Outros eliminado: produtos realocados em Fixação e Utilidades"
    outros = [n for n in nos if normalizar_nome(n["nome"]) == normalizar_nome("Outros")]
    for origem in outros:
        for filho in [n for n in nos if n.get("pai") == origem["id_alvo"]]:
            filho["pai"] = fixacao["id_alvo"]
        for cid in [origem.get("nuvemshop_id"), *(origem.get("nuvemshop_ids_mesclados") or [])]:
            if cid is None:
                continue
            mapa.setdefault(str(cid), {"nome": origem["nome"]}).update(
                id_alvo=fixacao["id_alvo"], motivo=motivo, reclassificar=False,
            )
        tax.setdefault("mesclagens", []).append({
            "tipo": "outros", "destino": fixacao["id_alvo"],
            "destino_nuvemshop_id": fixacao.get("nuvemshop_id"),
            "origem": origem["id_alvo"], "origem_nuvemshop_id": origem.get("nuvemshop_id"),
            "origem_nome": origem["nome"], "motivo": motivo,
        })
        nos.remove(origem)
    tinha_outros = bool(outros)
    for info in mapa.values():
        if normalizar_nome(info.get("nome") or "") != normalizar_nome("Outros"):
            continue
        if info.get("id_alvo"):
            tinha_outros = True
            continue
        info.update(id_alvo=fixacao["id_alvo"], motivo=motivo, reclassificar=False)
        tinha_outros = True
    if not tinha_outros:
        return
    existentes = {normalizar_nome(n["nome"]) for n in nos}
    for nome in _FOLHAS_FIXACAO:
        chave = normalizar_nome(nome)
        if chave in existentes:
            continue
        no = _no(_novo_id(nome, usados), nome, fixacao["id_alvo"])
        no["origem"] = ["v3"]
        nos.append(no)
        existentes.add(chave)


def organizar_v3(tax: dict) -> dict:
    """Coloca a árvore sob as 7 macro-raízes, sem passar de TAXONOMIA_MAX_NIVEIS.

    Sensores (e a árvore de detecção) entram inteiras em Automação e Controle.
    Outros deixa de existir: filhas e produtos vão para Fixação e Utilidades.
    """
    nos: list[dict] = tax["nos"]
    usados = {n["id_alvo"] for n in nos}
    macros = {
        nome: _adotar_macro(nos, nome, _ALIAS_DA_MACRO.get(nome, set()), usados)
        for nome in MACRO_RAIZES
    }
    ids_macro = {m["id_alvo"] for m in macros.values()}
    sem_macro: list[str] = []
    for no in list(nos):
        if no.get("pai") or no["id_alvo"] in ids_macro:
            continue
        if normalizar_nome(no["nome"]) == normalizar_nome("Outros"):
            continue
        destino = _destino_macro(no["nome"])
        if destino is None or _altura(nos, no["id_alvo"]) + 1 > TAXONOMIA_MAX_NIVEIS:
            sem_macro.append(no["nome"])
            continue
        no["pai"] = macros[destino]["id_alvo"]
    _realocar_outros(tax, macros["Fixação e Utilidades"], usados)
    por = {n["id_alvo"]: n for n in tax["nos"]}
    tax["profundidade_max"] = max((len(cadeia_ate_raiz(por, i)) for i in por), default=0)
    tax["raizes_sem_macro"] = sem_macro
    tax["proposta_v3"] = True
    return tax


def _por_nome_nos(tax: dict) -> dict[str, dict]:
    return {n["nome"]: n for n in tax["nos"]}


def aplicar_ajustes_v3(tax: dict) -> dict:
    """Decisões do operador sobre a v3, aplicadas em cima da árvore gerada.

    Transformadores sobem para Materiais Elétricos. Tiristores vão para
    Materiais Elétricos > Semicondutores. As duas grafias de DPS viram um nó.
    Os três nomes em inglês são traduzidos. A lista de fora do ramo volta.
    """
    por = _por_nome_nos(tax)
    me = por.get("Materiais Elétricos")
    dps = por.get("Proteção contra Surtos (DPS)")
    sob = por.get("Proteção contra sobretensão")
    if dps and sob and dps is not sob:
        tax = editar(tax, "mesclar", origem=sob["id_alvo"], destino=dps["id_alvo"],
                     motivo="mesma proteção contra surtos")
        por = _por_nome_nos(tax)
        me = por.get("Materiais Elétricos")
    if me:
        trafo = por.get("Transformadores")
        if trafo and trafo.get("pai") != me["id_alvo"]:
            tax = editar(tax, "mover", id_alvo=trafo["id_alvo"], pai=me["id_alvo"])
            por = _por_nome_nos(tax)
        tir = por.get("Tiristores")
        if tir:
            semi = por.get("Semicondutores")
            if semi is None:
                tax = editar(tax, "adicionar", nome="Semicondutores", pai=me["id_alvo"])
                por = _por_nome_nos(tax)
                semi = por["Semicondutores"]
            if tir.get("pai") != semi["id_alvo"]:
                tax = editar(tax, "mover", id_alvo=tir["id_alvo"], pai=semi["id_alvo"])
                por = _por_nome_nos(tax)
    for antigo, novo in TRADUCOES_V3:
        no = por.get(antigo)
        if no:
            tax = editar(tax, "renomear", id_alvo=no["id_alvo"], nome=novo)
            por = _por_nome_nos(tax)
    tax = editar(tax, "fora_do_ramo", termos=list(TERMOS_FORA_DO_RAMO))
    por_id = {n["id_alvo"]: n for n in tax["nos"]}
    tax["profundidade_max"] = max((len(cadeia_ate_raiz(por_id, i)) for i in por_id), default=0)
    return tax


# ---------------------------------------------------------------- geração
def gerar_proposta(snapshot: dict, wago: Counter | None = None) -> dict:
    cats = {c["id"]: c for c in snapshot["categorias"]}
    filhos: dict[int, list[int]] = defaultdict(list)
    for c in cats.values():
        if c["parent_id"] in cats:
            filhos[c["parent_id"]].append(c["id"])

    def cadeia(cid: int) -> list[int]:
        """[raiz, ..., cid]"""
        caminho, atual = [], cid
        while atual in cats and atual not in caminho:
            caminho.append(atual)
            atual = cats[atual]["parent_id"]
        return list(reversed(caminho))

    diretos: Counter = Counter()
    for p in snapshot["produtos"]:
        for cid in p["categorias"]:
            diretos[cid] += 1

    def total(cid: int) -> int:
        return diretos[cid] + sum(total(f) for f in filhos.get(cid, []))

    # 1) Preserva a profundidade da loja até TAXONOMIA_MAX_NIVEIS.
    # Uma cadeia mais funda que o teto encosta no ancestral que a deixa no último nível.
    pai_novo: dict[int, int | None] = {}
    mapa: dict[int, int | None] = {}
    motivo_sem_alvo: dict[int, str] = {}
    cortados = 0
    for cid in cats:
        if set(motivos_nome_suspeito(cats[cid]["nome"])) & _MOTIVOS_DESCARTE:
            mapa[cid] = None
            motivo_sem_alvo[cid] = "nome inválido"
            continue
        mapa[cid] = cid
        cam = cadeia(cid)
        if len(cam) <= 1:
            pai_novo[cid] = None
        elif len(cam) > TAXONOMIA_MAX_NIVEIS:
            pai_novo[cid] = cam[TAXONOMIA_MAX_NIVEIS - 2]
            cortados += 1
        else:
            pai_novo[cid] = cam[-2]

    # 2) Mesclar duplicadas (mesmo nome normalizado) em uma sobrevivente.
    grupos: dict[str, list[int]] = defaultdict(list)
    for cid in pai_novo:
        grupos[normalizar_nome(cats[cid]["nome"])].append(cid)
    mesclados: dict[int, list[int]] = defaultdict(list)
    for ids in grupos.values():
        if len(ids) < 2:
            continue
        ids.sort(key=lambda i: (-total(i), len(cadeia(i)), i))
        sobrevivente = ids[0]
        for origem in ids[1:]:
            mesclados[sobrevivente].append(origem)
            mapa[origem] = sobrevivente
            pai_novo.pop(origem, None)

    def resolver(cid: int | None) -> int | None:
        vistos = set()
        while cid is not None and mapa.get(cid) not in (cid, None) and cid not in vistos:
            vistos.add(cid)
            cid = mapa[cid]
        return cid if cid is not None and mapa.get(cid) == cid else None

    for cid in list(pai_novo):
        pai = resolver(pai_novo[cid]) if pai_novo[cid] is not None else None
        pai_novo[cid] = None if pai == cid else pai

    def _ciclo(cid: int) -> bool:
        vistos = set()
        pai = pai_novo.get(cid)
        while pai is not None and pai not in vistos:
            if pai == cid:
                return True
            vistos.add(pai)
            pai = pai_novo.get(pai)
        return False

    for cid in list(pai_novo):
        if _ciclo(cid):
            pai_novo[cid] = None
    for cid in mapa:
        if mapa[cid] is not None:
            mapa[cid] = resolver(cid)

    # 3) Estimar produtos por nó (pela categoria final atual de cada produto).
    estimados: Counter = Counter()
    for p in snapshot["produtos"]:
        alvos = {mapa.get(cid) for cid in p["categorias"]} - {None}
        alvos |= {pai_novo[a] for a in alvos if pai_novo.get(a) is not None}  # pai soma descendentes
        for alvo in alvos:
            estimados[alvo] += 1

    # 4) Montar nós.
    usados: set[str] = set()
    id_alvo: dict[int, str] = {}
    nos: list[dict] = []
    mesclagens: list[dict] = []
    ordem = sorted(pai_novo, key=lambda c: (pai_novo[c] is not None, cats[c]["nome"].lower()))
    for cid in ordem:
        id_alvo[cid] = _novo_id(ajustar_nome(cats[cid]["nome"]), usados)
    for cid in ordem:
        c = cats[cid]
        # entre grafias equivalentes, prefere a acentuada ("Relés" > "Reles"); vira `renomear` na Fase 4
        nome = max(
            [c["nome"], *(cats[m]["nome"] for m in mesclados.get(cid, []))],
            key=lambda n: (sum(1 for ch in n if ord(ch) > 127), n == c["nome"]),
        ) if mesclados.get(cid) else c["nome"]
        nome = ajustar_nome(nome)  # tradução Wago + limite de caracteres
        no = _no(id_alvo[cid], nome, id_alvo.get(pai_novo[cid]) if pai_novo[cid] else None)
        mesclagens.extend(
            {"tipo": "duplicada", "destino": id_alvo[cid], "destino_nuvemshop_id": cid,
             "origem_nuvemshop_id": m, "origem_nome": cats[m]["nome"]}
            for m in mesclados.get(cid, [])
        )
        if nome != c["nome"]:
            no["nome_atual_loja"] = c["nome"]
        no["nuvemshop_id"] = cid
        no["nuvemshop_ids_mesclados"] = sorted(mesclados.get(cid, []))
        no["sinonimos"] = sorted({c["nome"], *(cats[m]["nome"] for m in mesclados.get(cid, []))} - {nome})
        no["origem"] = ["loja"] + (["agrupamento"] if mesclados.get(cid) else [])
        no["produtos_estimados"] = estimados[cid]
        no["alertas"] = motivos_nome_suspeito(nome)
        nos.append(no)

    # 5) Remover nós vazios (sem produtos e sem filhos com produtos).
    removidos: list[int] = []
    mudou = True
    while mudou:
        mudou = False
        filhos_alvo = Counter(n["pai"] for n in nos if n["pai"])
        for no in list(nos):
            if no["produtos_estimados"] == 0 and not filhos_alvo.get(no["id_alvo"]):
                nos.remove(no)
                removidos.append(no["nuvemshop_id"])
                mudou = True
                for cid, alvo in list(mapa.items()):
                    if alvo == no["nuvemshop_id"]:
                        mapa[cid] = None
                        motivo_sem_alvo[cid] = "vazia"

    # 6) Categorias da Wago (português): casa com nó existente ou cria raiz nova.
    indice = {}
    for no in nos:
        for nome in [no["nome"], *no["sinonimos"]]:
            indice.setdefault(normalizar_nome(nome), no)
    for nome_wago, qtd in sorted((wago or Counter()).items()):
        chave = normalizar_nome(nome_wago)
        if not chave:
            continue
        no = indice.get(chave) or indice.get(normalizar_nome(ajustar_nome(nome_wago)))
        if no is None:
            nome_pt = ajustar_nome(nome_wago)
            no = _no(_novo_id(nome_pt, usados), nome_pt[:1].upper() + nome_pt[1:], None)
            nos.append(no)
            indice[chave] = no
        if "wago" not in no["origem"]:
            no["origem"].append("wago")
        no["produtos_wago"] = no.get("produtos_wago", 0) + qtd
        if nome_wago != no["nome"] and nome_wago not in no["sinonimos"]:
            no["sinonimos"].append(nome_wago)

    mapa_loja = {
        str(cid): {"id_alvo": id_alvo.get(alvo) if alvo is not None else None,
                   "nome": cats[cid]["nome"],
                   "motivo": None if alvo is not None else motivo_sem_alvo.get(cid, "sem alvo")}
        for cid, alvo in mapa.items()
    }
    tax = {
        "status": STATUS_PENDENTE,
        "versao": None,
        "gerada_em": datetime.now().isoformat(timespec="seconds"),
        "snapshot_em": snapshot.get("gerado_em"),
        "max_niveis": TAXONOMIA_MAX_NIVEIS,
        "nos": nos,
        "mapa_categorias_loja": mapa_loja,
        "mesclagens": mesclagens,
    }

    # 7) Consolidação de árvores duplicadas (sobrevivente = mais subcategorias).
    for grupo in CONSOLIDACOES_ARVORE:
        tax = consolidar_arvores(tax, grupo)

    # 8) Categorias genéricas finais ("buracos negros") saem da árvore; produtos vão para a Fase 3.
    tax = remover_genericas_finais(tax)

    # 9) V3: 7 macro-raízes, Sensores sob Automação e Outros dissolvido em Fixação.
    tax = organizar_v3(tax)
    tax = aplicar_ajustes_v3(tax)
    tax["niveis_cortados"] = cortados

    for no in tax["nos"]:
        no["alertas"] = alertas_no(no, tax["nos"])
    tax["nos"].sort(key=lambda n: (n["pai"] or n["id_alvo"], n["pai"] is not None, n["nome"].lower()))
    nos = tax["nos"]
    tax["resumo"] = {
        "nos": len(nos),
        "raizes": sum(1 for n in nos if n["pai"] is None),
        "subcategorias": sum(1 for n in nos if n["pai"] is not None),
        "categorias_loja": len(cats),
        "raizes_dissolvidas": 0,
        "niveis_cortados": cortados,
        "profundidade_max": tax.get("profundidade_max"),
        "raizes_sem_macro": tax.get("raizes_sem_macro") or [],
        "categorias_mescladas": sum(len(v) for v in mesclados.values()),
        "arvores_consolidadas": sum(1 for m in tax["mesclagens"] if m["tipo"] == "arvore"),
        "categorias_vazias_removidas": len(removidos),
        "genericas_removidas": len(tax.get("genericas_removidas") or []),
        "produtos_para_reclassificar_genericas": sum(
            g["produtos_estimados"] for g in tax.get("genericas_removidas") or []
        ),
        "categorias_nome_invalido": sum(1 for m in motivo_sem_alvo.values() if m == "nome inválido"),
        "nos_somente_wago": sum(1 for n in nos if n["origem"] == ["wago"]),
        "nos_com_alerta": sum(1 for n in nos if n["alertas"]),
        "sugeridas_consolidacao": sum(1 for n in nos if _poucos_produtos(n)),
    }
    return tax


def _poucos_produtos(no: dict) -> bool:
    return no.get("produtos_estimados", 0) <= LIMITE_CONSOLIDACAO


def alertas_no(no: dict, nos: list[dict]) -> list[str]:
    alertas = [m for m in motivos_nome_suspeito(no["nome"]) if m != "nome muito longo"]
    tem_filhos = any(n.get("pai") == no["id_alvo"] for n in nos)
    if len(no["nome"]) > MAX_CHARS_CATEGORIA:
        alertas.append(f"nome acima de {MAX_CHARS_CATEGORIA} caracteres")
    if e_generica(no["nome"]) and not tem_filhos:
        alertas.append("categoria genérica: não pode receber produtos")
    qtd = no.get("produtos_simulados", no.get("produtos_estimados", 0))
    novo_sem_simulacao = not no.get("nuvemshop_id") and "produtos_simulados" not in no
    if qtd <= LIMITE_CONSOLIDACAO and not tem_filhos and not novo_sem_simulacao:
        alertas.append(f"poucos produtos (≤{LIMITE_CONSOLIDACAO}): sugerido mesclar/excluir")
    return alertas


MOTIVO_GENERICA = "categoria genérica removida: produtos reclassificados na Fase 3"


def remover_genericas_finais(tax: dict) -> dict:
    """Remove nós genéricos sem subcategorias (Acessórios, Outros, Diversos, Geral).

    Os produtos dessas categorias ficam sem alvo e são obrigatoriamente reclassificados
    pela descrição na Fase 3 (a blacklist impede que voltem para uma genérica).
    """
    com_filhos = {n["pai"] for n in tax["nos"] if n.get("pai")}
    removidos = [n for n in tax["nos"] if e_generica(n["nome"]) and n["id_alvo"] not in com_filhos]
    if not removidos:
        return tax
    ids = {n["id_alvo"] for n in removidos}
    mapa = {k: dict(v) for k, v in (tax.get("mapa_categorias_loja") or {}).items()}
    for info in mapa.values():
        if info.get("id_alvo") in ids:
            info["id_alvo"], info["motivo"], info["reclassificar"] = None, MOTIVO_GENERICA, True
    for n in removidos:
        for cid in [n.get("nuvemshop_id"), *n.get("nuvemshop_ids_mesclados", [])]:
            if cid is not None:
                mapa.setdefault(str(cid), {"nome": n["nome"]}).update(
                    id_alvo=None, motivo=MOTIVO_GENERICA, reclassificar=True
                )
    return {
        **tax,
        "nos": [n for n in tax["nos"] if n["id_alvo"] not in ids],
        "mapa_categorias_loja": mapa,
        "genericas_removidas": [
            {"nome": n["nome"], "nuvemshop_id": n.get("nuvemshop_id"),
             "nuvemshop_ids_mesclados": n.get("nuvemshop_ids_mesclados", []),
             "produtos_estimados": n.get("produtos_estimados", 0)}
            for n in removidos
        ],
    }


def consolidar_arvores(tax: dict, nomes: tuple[str, ...]) -> dict:
    """Mescla árvores-raiz equivalentes. Sobrevivente: mais subcategorias (desempate: produtos)."""
    chaves = {normalizar_nome(n) for n in nomes}
    raizes = [n for n in tax["nos"] if n["pai"] is None and normalizar_nome(n["nome"]) in chaves]
    if len(raizes) < 2:
        return tax
    filhos = Counter(n["pai"] for n in tax["nos"] if n["pai"])
    raizes.sort(key=lambda n: (-filhos[n["id_alvo"]], -n["produtos_estimados"], n["id_alvo"]))
    destino = raizes[0]
    for origem in raizes[1:]:
        tax = editar(tax, "mesclar", origem=origem["id_alvo"], destino=destino["id_alvo"],
                     tipo="arvore", motivo="árvores duplicadas")
    return tax


# ---------------------------------------------------------------- persistência
def _gravar(tax: dict, caminho: Path) -> Path:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(tax, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(caminho)
    return caminho


def _ler(caminho: Path) -> dict | None:
    return json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else None


def salvar_proposta(tax: dict) -> Path:
    nos = tax.get("nos") or []
    resumo = {
        **(tax.get("resumo") or {}),
        "nos": len(nos),
        "raizes": sum(1 for n in nos if not n.get("pai")),
        "subcategorias": sum(1 for n in nos if n.get("pai")),
        "nos_novos": sum(1 for n in nos if not n.get("nuvemshop_id")),
        "nos_com_alerta": sum(1 for n in nos if n.get("alertas")),
    }
    tax = {**tax, "resumo": resumo, "status": STATUS_PENDENTE,
           "editada_em": datetime.now().isoformat(timespec="seconds")}
    caminho = _gravar(tax, arq_proposta())
    caminho.with_suffix(".md").write_text(arvore_markdown(tax), encoding="utf-8")
    return caminho


def carregar_proposta() -> dict | None:
    return _ler(arq_proposta())


def carregar_aprovada() -> dict | None:
    tax = _ler(arq_aprovada())
    return tax if tax and tax.get("status") == STATUS_APROVADA else None


def status_taxonomia() -> dict:
    aprovada, proposta = carregar_aprovada(), carregar_proposta()
    return {
        "aprovada": bool(aprovada),
        "versao": aprovada.get("versao") if aprovada else None,
        "aprovado_em": aprovada.get("aprovado_em") if aprovada else None,
        "aprovado_por": aprovada.get("aprovado_por") if aprovada else None,
        "proposta_pendente": bool(proposta),
        "liberado_para_classificacao": bool(aprovada) and not proposta,
        "resumo_proposta": proposta.get("resumo") if proposta else None,
    }


def aprovar_taxonomia(aprovado_por: str) -> Path:
    proposta = carregar_proposta()
    if not proposta:
        raise SystemExit("Nenhuma proposta pendente de aprovação. Gere com `taxonomia gerar`.")
    erros = validar_taxonomia(proposta)
    if erros:
        raise SystemExit("Taxonomia inválida, corrija antes de aprovar:\n- " + "\n- ".join(erros[:20]))
    anterior = _ler(arq_aprovada())
    if anterior:
        pasta_historico().mkdir(parents=True, exist_ok=True)
        shutil.copy2(arq_aprovada(), pasta_historico() / f"taxonomia-v{anterior.get('versao') or 0}.json")
    agora = datetime.now().isoformat(timespec="seconds")
    aprovada = {
        **proposta, "status": STATUS_APROVADA,
        "versao": int((anterior or {}).get("versao") or 0) + 1,
        "aprovado_por": aprovado_por, "aprovado_em": agora,
    }
    caminho = _gravar(aprovada, arq_aprovada())
    arq_proposta().unlink(missing_ok=True)
    arq_proposta().with_suffix(".md").unlink(missing_ok=True)
    return caminho


def base_para_edicao() -> dict:
    """Proposta pendente, ou cópia da aprovada (em memória, sem gravar)."""
    proposta = carregar_proposta()
    if proposta:
        return proposta
    aprovada = carregar_aprovada()
    if not aprovada:
        raise SystemExit("Não há taxonomia aprovada nem proposta para editar.")
    return {**aprovada, "status": STATUS_PENDENTE, "base_versao": aprovada.get("versao")}


def iniciar_edicao() -> dict:
    """Editar a taxonomia aprovada abre nova proposta pendente e bloqueia a Fase 3."""
    proposta = base_para_edicao()
    if not arq_proposta().exists():
        salvar_proposta(proposta)
    return proposta


def exigir_taxonomia_aprovada() -> dict:
    st = status_taxonomia()
    if not st["aprovada"]:
        raise SystemExit("Fase 3 bloqueada: aprove a taxonomia base antes de classificar.")
    if st["proposta_pendente"]:
        raise SystemExit("Fase 3 bloqueada: há edições pendentes na taxonomia; aprove-as antes de classificar.")
    return carregar_aprovada()  # type: ignore[return-value]


# ---------------------------------------------------------------- edição
def editar(tax: dict, acao: str, **kw: Any) -> dict:
    """Edições do operador. Valida o resultado e devolve a taxonomia nova (status pendente)."""
    nos = [dict(n) for n in tax["nos"]]
    por_id = {n["id_alvo"]: n for n in nos}
    mapa = {k: dict(v) for k, v in (tax.get("mapa_categorias_loja") or {}).items()}
    mesclagens = [dict(m) for m in tax.get("mesclagens") or []]

    def pegar(ident: str) -> dict:
        if ident not in por_id:
            raise ValueError(f"nó inexistente: {ident}")
        return por_id[ident]

    if acao == "renomear":
        no = pegar(kw["id_alvo"])
        if no["nome"] not in no["sinonimos"]:
            no["sinonimos"] = [*no["sinonimos"], no["nome"]]
        if no.get("nuvemshop_id"):
            no.setdefault("nome_atual_loja", no["nome"])  # vira `renomear` no plano (mesmo ID)
        no["nome"] = kw["nome"].strip()
        no["palavras_chave"] = palavras_chave(no["nome"])
        if no.get("nome_atual_loja") == no["nome"]:
            no.pop("nome_atual_loja")
    elif acao == "mover":
        no = pegar(kw["id_alvo"])
        pai = kw.get("pai") or None
        if pai is not None:
            pegar(pai)
        no["pai"] = pai
    elif acao == "mesclar":
        origem, destino = pegar(kw["origem"]), pegar(kw["destino"])
        if origem is destino:
            raise ValueError("origem e destino iguais")
        _mesclar_nos(nos, mapa, mesclagens, origem, destino, kw.get("tipo"), kw.get("motivo"))
    elif acao == "excluir":
        no = pegar(kw["id_alvo"])
        if any(n["pai"] == no["id_alvo"] for n in nos):
            raise ValueError("mova ou exclua as subcategorias antes")
        nos.remove(no)
        for info in mapa.values():
            if info.get("id_alvo") == no["id_alvo"]:
                info["id_alvo"], info["motivo"], info["reclassificar"] = None, "excluída pelo operador", True
    elif acao == "adicionar":
        pai = kw.get("pai") or None
        if pai is not None:
            pegar(pai)
        no = _no(_novo_id(kw["nome"], set(por_id)), kw["nome"].strip(), pai)
        no["origem"] = [kw.get("origem_no") or "operador"]
        no["sinonimos"] = sorted({s.strip() for s in kw.get("sinonimos") or [] if s.strip()} - {no["nome"]})
        if kw.get("divisao_de"):
            # divisão: produtos hoje na categoria de origem são reavaliados entre origem e nova
            origem_div = pegar(kw["divisao_de"])
            if origem_div["id_alvo"] in {n.get("pai") for n in nos}:
                raise ValueError("divisão só é permitida a partir de uma categoria final")
            origem_div["reavaliar"] = True
            no["divisao_de"] = origem_div["id_alvo"]
        nos.append(no)
    elif acao == "sinonimos":
        no = pegar(kw["id_alvo"])
        no["sinonimos"] = sorted({*no["sinonimos"], *(s.strip() for s in kw.get("sinonimos") or [] if s.strip())}
                                 - {no["nome"]})
    elif acao == "fora_do_ramo":
        termos = sorted({*(tax.get("fora_do_ramo") or []), *(t.strip() for t in kw.get("termos") or [] if t.strip())})
        tax = {**tax, "fora_do_ramo": termos}
    else:
        raise ValueError(f"ação desconhecida: {acao}")

    nova = {**tax, "nos": nos, "mapa_categorias_loja": mapa, "mesclagens": mesclagens,
            "status": STATUS_PENDENTE}
    # recusa só problemas introduzidos por esta edição (o operador corrige um item por vez)
    novos = [e for e in validar_taxonomia(nova) if e not in set(validar_taxonomia(tax))]
    if novos:
        raise ValueError("; ".join(novos[:5]))
    for no in nos:
        no["alertas"] = alertas_no(no, nos)
    return nova


def editar_lote(tax: dict, edicoes: list[dict]) -> dict:
    """Aplica uma lista de edições {"acao": ..., ...} em ordem; para no primeiro erro (com índice)."""
    for i, ed in enumerate(edicoes, 1):
        params = {k: v for k, v in ed.items() if k not in ("acao", "comentario")}
        try:
            tax = editar(tax, ed["acao"], **params)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"edição #{i} ({ed.get('acao')}): {exc}") from exc
    return tax


def _mesclar_nos(
    nos: list[dict], mapa: dict, mesclagens: list[dict], origem: dict, destino: dict,
    tipo: str | None = None, motivo: str | None = None,
) -> None:
    """Mescla `origem` em `destino` (só na proposta). Na loja vira: reparentar → mover → excluir."""
    filhos_origem = [n for n in nos if n["pai"] == origem["id_alvo"]]
    novo_pai = destino["id_alvo"]
    for filho in filhos_origem:
        filho["pai"] = novo_pai
    destino["sinonimos"] = sorted({*destino["sinonimos"], origem["nome"], *origem["sinonimos"]} - {destino["nome"]})
    destino["nuvemshop_ids_mesclados"] = sorted(
        {*destino["nuvemshop_ids_mesclados"], *origem["nuvemshop_ids_mesclados"]}
        | ({origem["nuvemshop_id"]} if origem["nuvemshop_id"] else set())
    )
    destino["produtos_estimados"] += origem["produtos_estimados"]
    destino["origem"] = sorted({*destino["origem"], *origem["origem"], "agrupamento"})
    nos.remove(origem)
    mesclagens.append({
        "tipo": tipo or ("arvore" if filhos_origem else "categoria"),
        "destino": destino["id_alvo"], "destino_nuvemshop_id": destino["nuvemshop_id"],
        "origem": origem["id_alvo"], "origem_nuvemshop_id": origem["nuvemshop_id"],
        "origem_nome": origem["nome"], "subcategorias_movidas": [f["id_alvo"] for f in filhos_origem],
        "motivo": motivo or "operador",
    })
    for info in mapa.values():
        if info.get("id_alvo") == origem["id_alvo"]:
            info["id_alvo"] = destino["id_alvo"]
    # filhas com o mesmo nome sob o novo pai também são mescladas
    irmas: dict[str, dict] = {}
    for n in list(nos):
        if n["pai"] != novo_pai:
            continue
        chave = normalizar_nome(n["nome"])
        if chave in irmas:
            a, b = sorted([irmas[chave], n], key=lambda x: -x["produtos_estimados"])
            _mesclar_nos(nos, mapa, mesclagens, b, a, "duplicada", "mesma subcategoria após mesclagem")
            irmas[chave] = a
        else:
            irmas[chave] = n


# ---------------------------------------------------------------- relatório
def arvore_markdown(tax: dict) -> str:
    nos = tax["nos"]
    filhos: dict[str, list[dict]] = defaultdict(list)
    for n in nos:
        if n["pai"]:
            filhos[n["pai"]].append(n)
    r = tax.get("resumo") or {}
    linhas = [
        f"# Taxonomia proposta"
        f"{' v' + str(int(tax['base_versao']) + 1) if tax.get('base_versao') else ''} — {tax.get('status')}",
        *([f"Simulação sem IA: {tax['simulacao']['categorizados_auto']} auto ({tax['simulacao']['pct_auto']}%), "
           f"{tax['simulacao']['aguardando_revisao']} revisão, {tax['simulacao']['fora_do_ramo']} fora do ramo"]
          if tax.get("simulacao") else []),
        *([f"Fora do ramo (arquivamento opcional): {', '.join(tax['fora_do_ramo'])}"] if tax.get("fora_do_ramo") else []),
        f"Gerada em {tax.get('gerada_em')} (snapshot {tax.get('snapshot_em')})",
        "",
        " · ".join(f"{k.replace('_', ' ')}: {v}" for k, v in r.items()),
        "",
    ]

    def fmt(n: dict) -> str:
        extra = [f"{n['produtos_estimados']} prod."]
        if "produtos_simulados" in n:
            extra = [f"{n['produtos_simulados']} prod. simulados (antes {n['produtos_estimados']})"]
        if not n.get("nuvemshop_id"):
            extra.append("NOVA")
        if n.get("divisao_de"):
            extra.append(f"divisão de {n['divisao_de']}")
        if n.get("produtos_wago"):
            extra.append(f"{n['produtos_wago']} Wago")
        if n.get("nuvemshop_ids_mesclados"):
            extra.append(f"mescla {len(n['nuvemshop_ids_mesclados'])} categoria(s) da loja")
        if n["sinonimos"]:
            extra.append("sinônimos: " + ", ".join(n["sinonimos"][:4]))
        if n["alertas"]:
            extra.append("⚠ " + ", ".join(n["alertas"]))
        return f"{n['nome']} ({'; '.join(extra)})"

    def ramo(no: dict, nivel: int) -> None:
        prefixo = "  " * nivel
        marca = "**" if nivel == 0 else ""
        linhas.append(f"{prefixo}- {marca}{fmt(no)}{marca}")
        if nivel >= 2:
            return
        for filho in sorted(filhos.get(no["id_alvo"], []), key=lambda n: -n.get("produtos_estimados", 0)):
            ramo(filho, nivel + 1)

    for raiz in sorted((n for n in nos if not n["pai"]), key=lambda n: -n.get("produtos_estimados", 0)):
        ramo(raiz, 0)
    return "\n".join(linhas) + "\n"


def executar_geracao(snapshot_path: Path | None = None, log=print) -> Path:
    from wago.taxonomia.inventario import carregar_snapshot, ultimo_snapshot

    ensure_dirs()
    caminho = snapshot_path or ultimo_snapshot()
    if not caminho:
        raise SystemExit("Nenhum snapshot encontrado. Rode `taxonomia inventario` primeiro.")
    tax = gerar_proposta(carregar_snapshot(caminho), categorias_wago())
    erros = validar_taxonomia(tax)
    if erros:
        tax["erros_validacao"] = erros
    destino = salvar_proposta(tax)
    log(arvore_markdown(tax))
    log(f"Proposta salva (pendente de aprovação): {destino}")
    if erros:
        log(f"Atenção: {len(erros)} problema(s) de validação a corrigir antes de aprovar.")
    return destino
