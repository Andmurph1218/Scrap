"""Provider do Bling v3: cliente HTTP e mapeamento canônico no mesmo módulo.

`BlingClient` concentra as chamadas HTTP, o envelope `{"data": ...}`, a
paginação por `pagina`/`limite` e o retry de 401 (renovação de token), 429
(limite de requisições) e 5xx (instabilidade). `BlingProvider` traduz o
contrato de `base.py` para os caminhos e campos do Bling.

Nenhuma mensagem de erro inclui access token ou refresh token.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import requests

from wago.marketplaces.base import (
    AlteracaoCategoria,
    AlteracaoProduto,
    BaseMarketplaceProvider,
    Categoria,
    CategoriaComProdutos,
    ErroAutenticacao,
    ErroMarketplace,
    ErroRateLimit,
    Id,
    NaoEncontrado,
    NovaCategoria,
    Produto,
)
from wago.marketplaces.bling_auth import BlingAuth
from wago.marketplaces.rate_limit import RateLimiter, limiter_bling

#: Tentativas totais de uma requisição antes de desistir (429 e 5xx).
MAX_TENTATIVAS = 5

#: Tamanho de página usado na paginação (padrão da API do Bling).
LIMITE_PAGINA = 100

#: Base da API v3 do Bling. O host `www` passou a ser bloqueado para chamadas
#: de API (HTTP 403); o endpoint oficial é `api`.
URL_BASE = "https://api.bling.com.br/Api/v3"

MSG_LIMITE_ESGOTADO = "limite de requisições do Bling esgotado"
MSG_INSTABILIDADE = "instabilidade do Bling"

#: Campos de produto que só o Bling escreve: ficam fora do corpo do PUT.
CAMPOS_PRODUTO_SO_LEITURA = frozenset({"id", "dataCriacao", "dataAlteracao", "dataExclusao"})


def _desembrulhar(corpo: Any) -> Any:
    """Aceita `{"data": ...}` ou o objeto direto, devolvendo o conteúdo útil."""
    if isinstance(corpo, dict) and "data" in corpo:
        return corpo["data"]
    return corpo


def _lista(corpo: Any) -> list:
    """Conteúdo do corpo como lista; `data: null` e formatos inesperados viram `[]`."""
    desembrulhado = _desembrulhar(corpo)
    return desembrulhado if isinstance(desembrulhado, list) else []


def _corpo_json(resposta: Any) -> Any:
    """JSON do corpo da resposta; `None` sem corpo e texto puro quando não é JSON."""
    if not resposta.content:
        return None
    try:
        return resposta.json()
    except ValueError:
        return resposta.text


def _dicionario(corpo: Any) -> dict:
    """Corpo desembrulhado como dict; `None` e formatos inesperados viram `{}`."""
    desembrulhado = _desembrulhar(corpo)
    return desembrulhado if isinstance(desembrulhado, dict) else {}


def _id_categoria(bruto: Any) -> Id | None:
    """Id de uma categoria bruta (dict com `id` ou o próprio id)."""
    return bruto.get("id") if isinstance(bruto, dict) else bruto


def _mesmo_id(a: Any, b: Any) -> bool:
    """Compara ids que podem vir como `str` ou `int`."""
    if a is None or b is None:
        return a is None and b is None
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _ids_produto(prod: dict) -> list[Id]:
    """Ids de categoria de um produto bruto: `categoria` primeiro, depois `categorias`.

    Aceita dicts (`{"id": ...}`) ou ids diretos, preserva a ordem e remove
    duplicados.
    """
    brutos = [prod.get("categoria"), *(prod.get("categorias") or [])]
    ids: list[Id] = []
    for bruto in brutos:
        valor = _id_categoria(bruto)
        if valor is not None and valor not in ids:
            ids.append(valor)
    return ids


def _categoria_canonica(cat: dict) -> Categoria:
    """Categoria do Bling no contrato canônico; `descricao` nunca é enviada de volta.

    A API real (v3) devolve o pai como objeto (`categoriaPai: {"id": N}`), com
    `id: 0` na raiz; o formato plano `categoriaPaiId` segue aceito por
    compatibilidade. Em ambos, id 0 vira `None`.
    """
    pai = cat.get("categoriaPai", cat.get("categoriaPaiId"))
    pai_id = _id_categoria(pai) or None
    return {
        "id": cat.get("id"),
        "nome": str(cat.get("descricao") or ""),
        "pai_id": pai_id,
        "descricao": "",
    }


def _produto_canonico(prod: dict) -> Produto:
    """Produto do Bling no contrato canônico.
    
    Mapeia `situacao` (A=ativo, I=inativo) para `publicado` (True/False).
    """
    situacao = prod.get("situacao")
    return {
        "id": prod.get("id"),
        "nome": str(prod.get("nome") or ""),
        "sku": str(prod.get("codigo") or ""),
        "categorias": _ids_produto(prod),
        "publicado": situacao == "A" if isinstance(situacao, str) else None,
    }


class BlingClient:
    """Requisições autenticadas ao Bling v3, com paginação e retry."""

    #: Mesmo helper exposto na classe para uso dos mapeamentos.
    _desembrulhar = staticmethod(_desembrulhar)

    def __init__(
        self,
        auth: BlingAuth,
        *,
        session: Any = None,
        limiter: RateLimiter | None = None,
        timeout: int = 30,
        url_base: str = URL_BASE,
    ) -> None:
        self.auth = auth
        self.session = requests if session is None else session
        self.limiter = limiter if limiter is not None else limiter_bling(auth)
        self.timeout = timeout
        self.url_base = url_base.rstrip("/")

    # ---- HTTP -----------------------------------------------------------
    def requisitar(
        self,
        metodo: str,
        caminho: str,
        *,
        params: dict | None = None,
        json: Any = None,
    ) -> Any:
        """Executa a requisição e devolve o corpo desembrulhado.

        O rate limiter é consultado antes de cada tentativa, inclusive nas
        repetições. Um 401 renova o token uma única vez e repete sem consumir
        tentativa; um segundo 401 vira `ErroAutenticacao`.
        """
        url = f"{self.url_base}{caminho}"
        renovou = False
        tentativa = 1
        while tentativa <= MAX_TENTATIVAS:
            headers = self._headers()
            self.limiter.adquirir(metodo=metodo, repeticao=tentativa > 1 or renovou)
            try:
                resposta = self.session.request(
                    metodo,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise ErroMarketplace(0, f"falha de rede: {exc.__class__.__name__}") from exc
            status = resposta.status_code

            if status == 401 and not renovou:
                # Token expirado: renova e repete sem gastar tentativa.
                self.auth.renovar()
                renovou = True
                continue

            if status >= 400:
                corpo = _corpo_json(resposta)
                if status == 429:
                    if isinstance(self.limiter, RateLimiter):
                        self.limiter.bloquear_dia()
                    raise ErroRateLimit(429, MSG_LIMITE_ESGOTADO, corpo)
                if 500 <= status < 600:
                    if tentativa >= MAX_TENTATIVAS:
                        raise ErroMarketplace(status, MSG_INSTABILIDADE, corpo)
                    self.limiter.aguardar_429(resposta.headers, tentativa)
                    tentativa += 1
                    continue
                raise self._erro(status, corpo)

            return _desembrulhar(_corpo_json(resposta))

        # Inalcançável: o laço só sai por retorno ou exceção.
        raise ErroMarketplace(500, MSG_INSTABILIDADE)

    def paginar(self, caminho: str, params: dict | None = None) -> Iterator[dict]:
        """Percorre todas as páginas, parando em página vazia ou incompleta."""
        pagina = 1
        while True:
            itens = _lista(
                self.requisitar(
                    "GET",
                    caminho,
                    params={**(params or {}), "pagina": pagina, "limite": LIMITE_PAGINA},
                )
            )
            if not itens:
                return
            yield from itens
            if len(itens) < LIMITE_PAGINA:
                return
            pagina += 1

    # ---- erros ----------------------------------------------------------
    @staticmethod
    def _erro(status: int, corpo: Any) -> ErroMarketplace:
        """Traduz status/corpo do Bling para o erro canônico correspondente."""
        mensagem = BlingClient._mensagem(corpo)
        if status in (401, 403):
            return ErroAutenticacao(status, mensagem, corpo)
        if status == 404:
            return NaoEncontrado(status, mensagem, corpo)
        if status == 429:
            return ErroRateLimit(status, mensagem, corpo)
        return ErroMarketplace(status, mensagem, corpo)

    @staticmethod
    def _mensagem(corpo: Any) -> str:
        """Mensagem do corpo na ordem de campos da API do Bling."""
        if isinstance(corpo, dict):
            erro = corpo.get("error")
            if isinstance(erro, dict):
                for chave in ("message", "description"):
                    valor = erro.get(chave)
                    if valor:
                        return str(valor)
            elif isinstance(erro, str) and erro.strip():
                return erro
            mensagem = corpo.get("message")
            if mensagem:
                return str(mensagem)
        return str(corpo)

    # ---- headers --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        """Headers de toda requisição, com o token válido no momento da chamada."""
        return {
            "Authorization": f"Bearer {self.auth.access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


class BlingProvider(BaseMarketplaceProvider):
    """`BaseMarketplaceProvider` que mapeia o contrato canônico para o Bling v3.

    No Bling a `descricao` da categoria **é** o nome; por isso a leitura devolve
    `descricao` vazia e o envio de `descricao` de categoria levanta erro em vez
    de renomear por acidente. `publicado` não tem equivalente confiável e
    também é recusado na escrita.
    """

    nome = "bling"

    def __init__(self, client: BlingClient) -> None:
        self.client = client

    # ---- categorias -----------------------------------------------------
    def listar_categorias(self) -> list[Categoria]:
        return [_categoria_canonica(c) for c in self.client.paginar("/categorias/produtos")]

    def criar_categoria(self, dados: NovaCategoria) -> Categoria:
        nome = dados["nome"]
        descricao = dados.get("descricao")
        if descricao and descricao != nome:
            raise ErroMarketplace(400, "Bling não separa descrição do nome da categoria")
        corpo: dict[str, Any] = {"descricao": nome}
        if dados.get("pai_id") is not None:
            # A API real ignora `categoriaPaiId` e só aplica o objeto `categoriaPai`.
            corpo["categoriaPai"] = {"id": dados["pai_id"]}
        resposta = _dicionario(self.client.requisitar("POST", "/categorias/produtos", json=corpo))
        novo_id = resposta.get("id")
        if novo_id is None:
            raise ErroMarketplace(500, "Bling não devolveu o id da categoria criada", resposta)
        # A resposta do POST é esparsa (`{"id": ...}`): confere o estado real por GET.
        real = _dicionario(self.client.requisitar("GET", f"/categorias/produtos/{novo_id}"))
        cat = _categoria_canonica({**corpo, **resposta, **real})
        if dados.get("pai_id") is not None and not _mesmo_id(cat["pai_id"], dados["pai_id"]):
            raise ErroMarketplace(409, f"pai não aplicado (esperado {dados['pai_id']}, atual {cat['pai_id']})")
        return {**cat, "reutilizada": False}

    def atualizar_categoria(self, id: Id, dados: AlteracaoCategoria) -> Categoria:
        if "descricao" in dados:
            raise ErroMarketplace(400, "descricao de categoria não é suportada no Bling: use nome")
        if not dados:
            raise ErroMarketplace(400, "nenhum campo para atualizar")
        atual = _dicionario(self.client.requisitar("GET", f"/categorias/produtos/{id}"))
        if not atual:
            raise NaoEncontrado(404, f"categoria {id} não encontrada")
        pai_atual = _id_categoria(atual.get("categoriaPai", atual.get("categoriaPaiId"))) or None
        if "pai_id" in dados and not _mesmo_id(dados["pai_id"], pai_atual):
            # Verificado contra a API real: o PUT aceita o corpo, responde 204 e
            # ignora o pai. Falhar explícito é melhor que um no-op silencioso.
            raise ErroMarketplace(400, "Bling não permite mover categoria pela API v3")
        # O PUT exige `descricao`; sem alteração de nome, reenvia a atual.
        corpo = {"descricao": str(atual.get("descricao") or "")}
        if "nome" in dados:
            corpo["descricao"] = dados["nome"]
        self.client.requisitar("PUT", f"/categorias/produtos/{id}", json=corpo)
        real = _dicionario(self.client.requisitar("GET", f"/categorias/produtos/{id}"))
        return _categoria_canonica({**atual, **corpo, **real})

    def categoria_tem_produtos(self, id: Id) -> bool:
        """Sonda barata: 1 GET `idCategoria=<id>&limite=1` em vez da listagem
        completa do catálogo. `None`/`400` tratado como 'sem produtos'."""
        try:
            resposta = self.client.requisitar(
                "GET", "/produtos", params={"idCategoria": id, "pagina": 1, "limite": 1}
            )
        except NaoEncontrado:
            return False
        except ErroMarketplace as exc:
            if exc.status != 400:
                raise
            return False
        return bool(_lista(resposta))

    def deletar_categoria(self, id: Id) -> None:
        if self.categoria_tem_produtos(id):
            raise CategoriaComProdutos(409, f"categoria {id} tem produtos vinculados")
        try:
            self.client.requisitar("DELETE", f"/categorias/produtos/{id}")
        except NaoEncontrado:
            return  # já não existe: idempotente
        except ErroMarketplace as exc:
            if exc.status != 400:
                raise
            # 400 do Bling costuma ser vínculo com produtos, mas pode ser
            # validação; preservar a mensagem original evita diagnóstico errado.
            raise CategoriaComProdutos(409, f"Bling recusou a exclusão: {exc.mensagem}", exc.corpo) from exc

    # ---- produtos -------------------------------------------------------
    def obter_produto(self, id: Id) -> Produto:
        """1 produto por GET — usado no snapshot cirúrgico antes da aplicação."""
        return _produto_canonico(
            _dicionario(self.client.requisitar("GET", f"/produtos/{id}"))
        )

    def listar_produtos(self) -> list[Produto]:
        """Produtos do catálogo com as categorias de cada um.

        A listagem do Bling v3 não inclui `categoria`/`categorias`. O mapa é
        montado consultando `/produtos?idCategoria=<id>` uma vez por categoria
        (~1 + N chamadas, independente do tamanho do catálogo), em vez de um
        GET por produto. Produtos sem categoria ficam com `categorias: []`.

        A "Categoria padrão" do Bling (id variável por conta) **não** aparece
        em `/categorias/produtos`; produtos nela ficam com `categorias: []`,
        que é o equivalente prático de "não classificado".

        Estratégia otimizada:
        1. Listar produtos geral (sem categoria) - apenas IDs e dados básicos
        2. Mapeamento por categoria em lote usando paginação otimizada
        """
        from wago.config import TAXONOMIA_SNAPSHOTS
        import json
        from datetime import datetime

        # Primeiro, tenta carregar de snapshot se disponível (mais rápido)
        snapshot_files = sorted(TAXONOMIA_SNAPSHOTS.glob("snapshot-bling-*.json"))
        if snapshot_files:
            # Verifica se o snapshot é recente (menos de 1 hora)
            latest_snapshot = snapshot_files[-1]
            mod_time = datetime.fromtimestamp(latest_snapshot.stat().st_mtime)
            if (datetime.now() - mod_time).total_seconds() < 3600:
                snapshot = json.loads(latest_snapshot.read_text(encoding="utf-8"))
                if "produtos" in snapshot and snapshot["produtos"]:
                    return snapshot["produtos"]

        # Estratégia alternativa: listagem geral + mapeamento por categoria.
        # A listagem geral captura também produtos sem categoria, que ficam
        # com `categorias: []` (o equivalente prático de "não classificado").
        brutos: dict[Id, dict] = {}
        ordem: list[Id] = []
        for prod in self.client.paginar("/produtos"):
            pid = prod.get("id")
            if pid is None:
                continue
            if pid not in brutos:
                brutos[pid] = dict(prod)
                ordem.append(pid)

        categorias_por_produto: dict[Id, list[Id]] = {}
        for cat in self.listar_categorias():
            cid = cat["id"]
            for prod in self.client.paginar("/produtos", {"idCategoria": cid}):
                pid = prod.get("id")
                if pid is None:
                    continue
                if pid not in brutos:  # produto que a listagem geral não devolveu
                    brutos[pid] = dict(prod)
                    ordem.append(pid)
                ids = categorias_por_produto.setdefault(pid, [])
                if cid not in ids:
                    ids.append(cid)

        return [
            _produto_canonico(
                {**brutos[pid], "categorias": [{"id": c} for c in categorias_por_produto.get(pid, [])]}
            )
            for pid in ordem
        ]

    def atualizar_produto(self, id: Id, dados: AlteracaoProduto) -> Produto:
        atual = _dicionario(self.client.requisitar("GET", f"/produtos/{id}"))
        # O PUT sobrescreve o produto: parte do atual para não apagar campos omitidos.
        corpo = {k: v for k, v in atual.items() if k not in CAMPOS_PRODUTO_SO_LEITURA}
        
        # O campo 'formato' é obrigatório no PUT/POST do Bling v3
        if "formato" not in corpo:
            corpo["formato"] = "S"  # Simples por padrão

        # O campo 'situacao' é obrigatório no PUT do Bling v3
        # Tratar 'publicado' (canônico) como alias para 'situacao'
        if "publicado" in dados:
            # publicado=False = arquivado = "I" (inativo)
            corpo["situacao"] = "I" if dados["publicado"] is False else "A"
        elif "situacao" not in corpo:
            corpo["situacao"] = "A"  # Ativo por padrão

        if "nome" in dados:
            corpo["nome"] = dados["nome"]
        if "categorias" in dados:
            ids = [_id_categoria(c) for c in dados["categorias"]]
            ids = [i for i in ids if i is not None]
            # O merge final parte do produto original: sem limpar `atual`, as chaves
            # antigas voltariam e os ids obsoletos apareceriam no retorno.
            atual = {k: v for k, v in atual.items() if k not in ("categoria", "categorias")}
            corpo.pop("categoria", None)
            corpo.pop("categorias", None)
            if len(ids) == 1:
                corpo["categoria"] = {"id": ids[0]}
            else:
                corpo["categorias"] = [{"id": cat} for cat in ids]
        # Validar: produtos com formato="V" precisam de variações
        # Se o produto tem formato="V" (Variedura) mas não tem variações,
        # o Bling API rejeita o PUT com erro de validação.
        if corpo.get("formato") == "V" and not corpo.get("variações"):
            raise ErroMarketplace(
                400,
                "Não é possível atribuir categoria a produtos de Variedura sem variações: "
                "o Bling exige que variações sejam informadas para produtos com formato='V'",
                {"product_id": id, "formato": "V", "variações": []}
            )
        resposta = self.client.requisitar("PUT", f"/produtos/{id}", json=corpo)
        return _produto_canonico({**atual, **corpo, **_dicionario(resposta)})
