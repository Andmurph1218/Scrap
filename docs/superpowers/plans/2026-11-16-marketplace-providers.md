# Marketplace Providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adicionar uma camada de providers de marketplace, adaptar a Nuvemshop e entregar o provider do Bling ERP v3 pronto para receber a taxonomia v8.

**Architecture:** Pacote novo e aditivo `src/wago/marketplaces/`. `base.py` define o contrato canônico e os erros; `nuvemshop_provider.py` compõe o `NuvemshopAPI` existente; `bling_auth.py` cuida do OAuth 2.0 com tokens em arquivo restrito; `bling_provider.py` separa HTTP (`BlingClient`, paginação/retry) de mapeamento (`BlingProvider`). Nada em `taxonomia/aplicar.py`, `nuvemshop_api.py` ou `inventario.py` muda de comportamento.

**Tech Stack:** Python 3.11+, `requests`, `python-dotenv`, `pytest`, `argparse`. Sem async e sem dependências novas.

**Spec:** `docs/superpowers/specs/2026-11-16-marketplace-providers-design.md`

## Global Constraints

- Python 3.11+; dependências existentes apenas (`requests`, `python-dotenv`). **Não** adicionar `cryptography`.
- Bling v3: base `https://www.bling.com.br/Api/v3`, 3 requisições/s, 120.000/dia, tratar HTTP 429.
- Tokens do Bling em `data/state/bling_tokens.json` (já ignorado pelo Git): escrita atômica, `chmod 0600`, nenhum token em log, exceção ou mensagem.
- Não alterar o comportamento de `src/wago/taxonomia/nuvemshop_api.py`, `aplicar.py` ou `inventario.py`; a abstração é aditiva.
- Nomes e mensagens em português, seguindo o projeto (`listar_categorias`, `criar_categoria`, `atualizar_produto`, ...).
- Testes rodam da raiz do repo: `& ".venv\Scripts\python.exe" -m pytest -q`.
- Nenhuma chamada real às APIs nos testes; tudo com sessões e clientes falsos.
- Campos canônicos exatamente como no spec: `Categoria{id,nome,pai_id,descricao}`, `NovaCategoria{nome,pai_id,descricao}`, `AlteracaoCategoria`, `Produto{id,nome,sku,categorias,publicado}`, `AlteracaoProduto{categorias,publicado,nome}`.

## Review Focus

1. **Respostas vazias/nulas do Bling** (`data: null`, `data: []`, lista vazia): `listar_categorias`/`listar_produtos` devem devolver `[]` e `paginar` deve parar sem erro. Teste em Task 5 e Task 6.
2. **Campos ausentes no JSON do Bling** (produto sem `codigo`, sem `categoria`/`categorias`; categoria com `descricao` nula): mapear para `""` e `[]`, sem `KeyError`. Teste em Task 6.
3. **`publicado` da Nuvemshop quando `visibility` e `published` discordam ou `visibility` falta**: refletir a vitrine (`visibility="hidden"` → `False`; sem `visibility`, usar `published`). Teste em Task 3.
4. **Arquivo de token corrompido/ausente e refresh recusado**: levantar `ErroAutenticacao` claro, preservar os tokens anteriores e não vazar segredo na mensagem. Teste em Task 4.
5. **HTTP 429 sem `Retry-After`, com valor inválido e cota diária esgotada**: aplicar backoff, limitar a 5 tentativas e levantar `ErroRateLimit` ao esgotar — nunca laço infinito. Teste em Task 2 e Task 5.

---

### Task 1: Contrato canônico e erros

**Files:**
- Create: `src/wago/marketplaces/__init__.py`
- Create: `src/wago/marketplaces/base.py`
- Test: `tests/test_marketplaces_base.py`

**Interfaces:**
- Consumes: nada.
- Produces: `Id = str | int`; `Categoria`, `NovaCategoria`, `AlteracaoCategoria`, `Produto`, `AlteracaoProduto` (TypedDicts); `ErroMarketplace(status, mensagem, corpo=None)`, `ErroAutenticacao`, `ErroRateLimit`, `CategoriaComProdutos`, `NaoEncontrado`; `BaseMarketplaceProvider` com os seis métodos abstratos do spec.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_marketplaces_base.py
import pytest

from wago.marketplaces.base import (
    AlteracaoCategoria, AlteracaoProduto, BaseMarketplaceProvider, Categoria,
    CategoriaComProdutos, ErroAutenticacao, ErroMarketplace, ErroRateLimit, NaoEncontrado, Produto,
)


class Completo(BaseMarketplaceProvider):
    nome = "fake"

    def listar_categorias(self): return []
    def criar_categoria(self, dados): return {"id": 1, "nome": dados["nome"], "pai_id": None, "descricao": ""}
    def atualizar_categoria(self, id, dados): return {"id": id, "nome": "x", "pai_id": None, "descricao": ""}
    def deletar_categoria(self, id): return None
    def listar_produtos(self): return []
    def atualizar_produto(self, id, dados): return {"id": id, "nome": "x", "sku": "", "categorias": [], "publicado": None}


def test_provider_incompleto_nao_instancia():
    class Incompleto(BaseMarketplaceProvider):
        pass

    with pytest.raises(TypeError):
        Incompleto()


def test_provider_completo_instancia_e_expoe_nome():
    assert Completo().nome == "fake"


@pytest.mark.parametrize("tipo", [ErroAutenticacao, ErroRateLimit, CategoriaComProdutos, NaoEncontrado])
def test_erros_derivam_de_erro_marketplace_com_status_e_corpo(tipo):
    exc = tipo(401, "sem token", {"erro": "x"})
    assert isinstance(exc, ErroMarketplace)
    assert exc.status == 401
    assert exc.corpo == {"erro": "x"}
    assert str(exc) == "HTTP 401: sem token"


def test_typed_dicts_tem_as_chaves_do_spec():
    assert Categoria.__required_keys__ == frozenset({"id", "nome", "pai_id", "descricao"})
    assert Produto.__required_keys__ == frozenset({"id", "nome", "sku", "categorias", "publicado"})
    assert AlteracaoCategoria.__optional_keys__ == frozenset({"nome", "pai_id", "descricao"})
    assert AlteracaoProduto.__optional_keys__ == frozenset({"categorias", "publicado", "nome"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_marketplaces_base.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wago.marketplaces'`.

- [ ] **Step 3: Implement `base.py` and the empty package**

Create `src/wago/marketplaces/__init__.py` with a docstring only. In `base.py`:

- `Id = str | int`.
- `Categoria` includes `reutilizada: NotRequired[bool]` (import de `typing`), para o retorno de `criar_categoria`; os demais TypedDicts exatamente como no spec.
- `ErroMarketplace(Exception)` guarda `status`, `mensagem`, `corpo` e monta `HTTP {status}: {mensagem}`; os quatro subtipos não adicionam estado.
- `BaseMarketplaceProvider(ABC)` com `nome: str = ""` e os seis `@abstractmethod` com as assinaturas exatas do spec.

- [ ] **Step 4: Run test to verify it passes**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_marketplaces_base.py -q`
Expected: PASS (7 tests: 2 de instanciação, 4 de erros parametrizados, 1 de chaves dos TypedDicts).

- [ ] **Step 5: Commit**

```bash
git add src/wago/marketplaces/__init__.py src/wago/marketplaces/base.py tests/test_marketplaces_base.py
git commit -m "feat(marketplaces): contrato canonico e erros de marketplace"
```

---

### Task 2: Rate limiter síncrono do Bling

**Files:**
- Create: `src/wago/marketplaces/rate_limit.py`
- Test: `tests/test_bling_rate_limit.py`

**Interfaces:**
- Consumes: `ErroRateLimit` de `wago.marketplaces.base`.
- Produces:
  - `RateLimiter(por_segundo: int = 3, por_dia: int = 120_000, *, relogio: Callable[[], float] = time.monotonic, dormir: Callable[[float], None] = time.sleep)`
  - `adquirir() -> None` — espaça chamadas e conta a cota diária.
  - `aguardar_429(headers: Mapping[str, str], tentativa: int) -> float` — dorme e devolve o atraso usado.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bling_rate_limit.py
import pytest

from wago.marketplaces.base import ErroRateLimit
from wago.marketplaces.rate_limit import RateLimiter


class Relogio:
    def __init__(self, agora=100.0):
        self.agora = agora

    def __call__(self):
        return self.agora


def _limiter(por_dia=120_000):
    relogio, dormidas = Relogio(), []
    limiter = RateLimiter(por_dia=por_dia, relogio=relogio, dormir=lambda s: dormidas.append(s))
    return limiter, relogio, dormidas


def test_espaca_no_minimo_de_034s_entre_requisicoes():
    limiter, _relogio, dormidas = _limiter()
    limiter.adquirir()
    limiter.adquirir()
    assert dormidas == [0.34]


def test_cota_diaria_esgotada_levanta_erro_rate_limit():
    limiter, _relogio, _dormidas = _limiter(por_dia=2)
    limiter.adquirir()
    limiter.adquirir()
    with pytest.raises(ErroRateLimit) as exc:
        limiter.adquirir()
    assert exc.value.status == 429


def test_janela_diaria_reinicia_apos_24h():
    limiter, relogio, _dormidas = _limiter(por_dia=2)
    limiter.adquirir()
    limiter.adquirir()
    relogio.agora += 24 * 60 * 60
    limiter.adquirir()
    assert limiter.requisicoes_dia == 1


def test_retry_after_valido_define_o_atraso():
    limiter, _relogio, dormidas = _limiter()
    assert limiter.aguardar_429({"Retry-After": "2"}, 1) == 2.0
    assert dormidas == [2.0]


def test_retry_after_invalido_cai_no_backoff_exponencial():
    limiter, _relogio, dormidas = _limiter()
    assert limiter.aguardar_429({"Retry-After": "amanhã"}, 0) == 0.34
    assert limiter.aguardar_429({}, 2) == 1.36
    assert dormidas == [0.34, 1.36]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_bling_rate_limit.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wago.marketplaces.rate_limit'`.

- [ ] **Step 3: Implement `RateLimiter`**

- `self.intervalo = max(1.0 / por_segundo, 0.34)`.
- `self._ultima: float | None = None`, `self.requisicoes_dia = 0`, `self._inicio_dia: float | None = None`.
- `adquirir()`: se `_inicio_dia is None` ou `relogio() - _inicio_dia >= 86400`, zera contador e início. Se `requisicoes_dia >= por_dia`, levanta `ErroRateLimit(429, "cota diária do Bling esgotada")`. Se `_ultima is not None`, dorme `max(0.0, intervalo - (agora - _ultima))`. Atualiza `_ultima` e incrementa `requisicoes_dia`.
- `aguardar_429()`: tenta `float(headers.get("Retry-After", ""))`; se falhar ou não existir, usa `0.34 * 2 ** tentativa`. Dorme e devolve o valor.

- [ ] **Step 4: Run test to verify it passes**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_bling_rate_limit.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/wago/marketplaces/rate_limit.py tests/test_bling_rate_limit.py
git commit -m "feat(marketplaces): rate limiter sincrono 3/s e 120k/dia"
```

---

### Task 3: `NuvemshopProvider` (adapter)

**Files:**
- Create: `src/wago/marketplaces/nuvemshop_provider.py`
- Test: `tests/test_nuvemshop_provider.py`

**Interfaces:**
- Consumes: `BaseMarketplaceProvider`, `Categoria`, `Produto`, `NovaCategoria`, `AlteracaoCategoria`, `AlteracaoProduto`, `ErroMarketplace`, `ErroAutenticacao`, `NaoEncontrado`, `ErroRateLimit`; `NuvemshopAPI`, `ErroNuvemshop`, `texto_i18n`.
- Produces:
  - `NuvemshopProvider(api: NuvemshopAPI | None = None)`; `nome = "nuvemshop"`.
  - `_publicado(prod: dict) -> bool | None` e `_sku(prod: dict) -> str` como funções de módulo (testáveis).
  - `traduzir_erro(exc: ErroNuvemshop) -> ErroMarketplace`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_nuvemshop_provider.py
import pytest

from wago.marketplaces.base import ErroMarketplace
from wago.marketplaces.nuvemshop_provider import NuvemshopProvider, _publicado, _sku
from wago.taxonomia.nuvemshop_api import ErroNuvemshop, NuvemshopAPI


class Resp:
    def __init__(self, status, corpo=None, headers=None):
        self.status_code = status
        self._corpo = corpo
        self.headers = headers or {}
        self.content = b"x" if corpo is not None else b""
        self.text = ""


class FakeSession:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def request(self, metodo, url, headers=None, params=None, json=None, timeout=None):
        self.chamadas.append({"metodo": metodo, "url": url, "json": json})
        return self.respostas.pop(0)


def _api(respostas):
    return NuvemshopAPI("123", "tok", session=FakeSession(respostas), dormir=lambda _s: None)


def test_publicado_hidden_vence_published_true():
    assert _publicado({"visibility": "hidden", "published": True}) is False


def test_publicado_sem_visibility_usa_published_como_fallback():
    assert _publicado({"published": False}) is False
    assert _publicado({}) is None


def test_sku_pega_a_primeira_variante_com_codigo():
    assert _sku({"variants": [{"sku": ""}, {"sku": " A-1 "}]}) == "A-1"
    assert _sku({}) == ""


def test_listar_categorias_converte_i18n_e_parent():
    api = _api([Resp(200, [{"id": 10, "name": {"pt": "Materiais"}, "parent": None, "description": {"pt": "Desc"}}])])
    assert NuvemshopProvider(api).listar_categorias() == [
        {"id": 10, "nome": "Materiais", "pai_id": None, "descricao": "Desc"}
    ]


def test_listar_produtos_mapeia_sku_categorias_e_publicado():
    api = _api([Resp(200, [{
        "id": 1, "name": {"pt": "Contator"}, "categories": [{"id": 21}, 10],
        "variants": [{"sku": "SKU-1"}], "visibility": "hidden", "published": True,
    }])])
    assert NuvemshopProvider(api).listar_produtos() == [
        {"id": 1, "nome": "Contator", "sku": "SKU-1", "categorias": [21, 10], "publicado": False}
    ]


def test_criar_categoria_repassa_descricao_e_marca_reutilizada():
    api = _api([Resp(201, {"id": 33, "name": {"pt": "Canaletas"}, "parent": 10, "description": {"pt": "d"}})])
    criada = NuvemshopProvider(api).criar_categoria({"nome": "Canaletas", "pai_id": 10, "descricao": "d"})
    assert criada["reutilizada"] is False
    assert criada["pai_id"] == 10
    assert api.session.chamadas[0]["json"]["description"] == {"pt": "d"}


def test_atualizar_categoria_envia_name_i18n_e_parent():
    api = _api([
        Resp(200, {"id": 33, "name": {"pt": "Antigo"}, "parent": None}),
        Resp(200, {"id": 33, "name": {"pt": "Novo"}, "parent": 10}),
    ])
    NuvemshopProvider(api).atualizar_categoria(33, {"nome": "Novo", "pai_id": 10})
    enviado = api.session.chamadas[1]["json"]
    assert enviado["name"] == {"pt": "Novo"} and enviado["parent"] == 10


def test_deletar_categoria_404_e_idempotente():
    api = _api([Resp(404, {"description": "Not Found"})])
    NuvemshopProvider(api).deletar_categoria(99)  # não levanta


def test_atualizar_produto_move_categorias():
    api = _api([Resp(200, {"id": 1, "categories": [{"id": 21}], "published": True, "visibility": "visible"})])
    NuvemshopProvider(api).atualizar_produto(1, {"categorias": [21]})
    assert api.session.chamadas[0]["json"]["categories"] == [21]


def test_atualizar_produto_arquiva_com_visibility_e_cai_para_published_em_422():
    api = _api([
        Resp(422, {"description": "invalid visibility"}),
        Resp(200, {"id": 1, "published": False, "visibility": "hidden"}),
    ])
    produto = NuvemshopProvider(api).atualizar_produto(1, {"publicado": False})
    assert api.session.chamadas[0]["json"]["visibility"] == "hidden"
    assert api.session.chamadas[1]["json"]["published"] is False
    assert produto["publicado"] is False


def test_atualizar_produto_renomeia_preservando_os_outros_idiomas():
    api = _api([
        Resp(200, {"id": 1, "name": {"pt": "Antigo", "es": "Viejo", "en": "Old"}}),
        Resp(200, {"id": 1, "name": {"pt": "Novo", "es": "Viejo", "en": "Old"}}),
    ])
    NuvemshopProvider(api).atualizar_produto(1, {"nome": "Novo"})
    assert api.session.chamadas[1]["json"]["name"] == {"pt": "Novo", "es": "Viejo", "en": "Old"}


def test_atualizar_produto_recusa_sku_nivel_produto():
    api = _api([])
    with pytest.raises(ErroMarketplace) as exc:
        NuvemshopProvider(api).atualizar_produto(1, {"sku": "NOVO"})
    assert exc.value.status == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_nuvemshop_provider.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wago.marketplaces.nuvemshop_provider'`.

- [ ] **Step 3: Implement `NuvemshopProvider`**

- `__init__(api=None)`: `self.api = api or NuvemshopAPI.do_env()`.
- `traduzir_erro`: 401/403 → `ErroAutenticacao`, 404 → `NaoEncontrado`, 429 → `ErroRateLimit`, senão `ErroMarketplace`, sempre copiando `status`, mensagem (`str(exc)`) e `corpo`.
- `_publicado`: se `visibility` estiver presente, `return prod["visibility"] != "hidden"`; se `published` estiver presente, `bool(...)`; senão `None`.
- `_sku`: primeira variante dict com `sku` não vazio, sem espaços.
- `listar_categorias`: envolve `self.api.listar_categorias()` em `try/except ErroNuvemshop` e mapeia `{id, nome: texto_i18n(name), pai_id: parent or None, descricao: texto_i18n(description)}`.
- `criar_categoria(dados)`: `kwargs = {}`; se `dados.get("descricao")`, `kwargs["description"] = {"pt": dados["descricao"]}`; chama `self.api.criar_categoria(dados["nome"], dados.get("pai_id"), **kwargs)`; devolve o mapa com `reutilizada`.
- `atualizar_categoria(id, dados)`: `kwargs` com `name={"pt": dados["nome"]}` quando `nome` presente, `parent=dados["pai_id"]` quando a chave `pai_id` existe, `description={"pt": ...}` quando `descricao` não vazia; chama `self.api.atualizar_categoria(id, **kwargs)`; se a resposta não trouxer `name`, faz `self.api.obter_categoria(id)`.
- `deletar_categoria`: `self.api.excluir_categoria(id)`; traduz erros que não sejam 404.
- `listar_produtos`: mapeia `id`, `nome=texto_i18n(name)`, `sku=_sku`, `categorias` aceitando ints ou dicts (`c["id"]`), `publicado=_publicado`.
- `atualizar_produto(id, dados)`:
  - `sku` em `dados` → `ErroMarketplace(400, "sku não é atualizável no nível do produto")`.
  - Monta `kwargs` com `categories=list(dados["categorias"])` quando presente; com `nome` presente, `atual = self.api.obter_produto(id)` e `kwargs["name"] = {**atual["name"], "pt": dados["nome"]}` se `name` for dict, senão `{"pt": dados["nome"]}`.
  - `publicado=False`: tenta `atualizar_produto(id, **kwargs, visibility="hidden")`; em `ErroNuvemshop` 422, repete com `published=False`. Depois, se `resultado.get("published") is not False`, confirma por `obter_produto(id)` e levanta `ErroMarketplace(409, "produto continua publicado")` se ainda estiver publicado.
  - `publicado=True`: envia `visibility="visible"`.
  - Sem `publicado`: um único PUT.
  - Devolve o produto mapeado. Se a resposta do PUT não for um dicionário ou vier vazia, confirma com `obter_produto(id)`; não faz GET extra quando a resposta já tem campos, para não quebrar a paridade de chamadas.
- Todos os métodos traduzem `ErroNuvemshop` com `traduzir_erro`.

- [ ] **Step 4: Run test to verify it passes**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_nuvemshop_provider.py -q`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add src/wago/marketplaces/nuvemshop_provider.py tests/test_nuvemshop_provider.py
git commit -m "feat(marketplaces): adapter Nuvemshop sobre a API existente"
```

---

### Task 4: OAuth do Bling e armazenamento de tokens

**Files:**
- Create: `src/wago/marketplaces/bling_auth.py`
- Test: `tests/test_bling_auth.py`

**Interfaces:**
- Consumes: `ErroAutenticacao`; `wago.config.env`, `wago.config.STATE`.
- Produces:
  - `TokenStore(caminho: Path | None = None)`; `carregar() -> dict | None`; `salvar(tokens: dict) -> None`.
  - `BlingAuth(store: TokenStore | None = None, *, session=None, dormir=time.sleep, relogio=time.time)`.
  - `url_autorizacao(state: str | None = None) -> str`; `trocar_codigo(code: str) -> dict`; `access_token() -> str`; `renovar() -> dict`.
  - Constantes `URL_AUTORIZACAO`, `URL_TOKEN`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bling_auth.py
import json

import pytest

from wago.marketplaces.base import ErroAutenticacao
from wago.marketplaces.bling_auth import BlingAuth, TokenStore


class Resp:
    def __init__(self, status, corpo=None):
        self.status_code = status
        self._corpo = corpo
        self.content = b"x" if corpo is not None else b""
        self.text = json.dumps(corpo or {})

    def json(self):
        return self._corpo


class Sessao:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def post(self, url, data=None, auth=None, timeout=None):
        self.chamadas.append({"url": url, "data": dict(data or {}), "auth": auth})
        return self.respostas.pop(0)


@pytest.fixture
def store(tmp_path):
    return TokenStore(tmp_path / "bling_tokens.json")


@pytest.fixture
def credenciais(monkeypatch):
    monkeypatch.setenv("BLING_CLIENT_ID", "cid")
    monkeypatch.setenv("BLING_CLIENT_SECRET", "segredo")
    monkeypatch.delenv("BLING_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BLING_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("BLING_REDIRECT_URI", raising=False)


def test_salvar_e_carregar_tokens_grava_atualizado_em(store):
    store.salvar({"access_token": "a", "refresh_token": "r", "expira_em": 123})
    tokens = store.carregar()
    assert tokens["access_token"] == "a" and tokens["refresh_token"] == "r"
    assert "atualizado_em" in tokens
    assert not (store.caminho.parent / "bling_tokens.tmp").exists()


def test_arquivo_corrompido_levanta_erro_sem_apagar(tmp_path):
    caminho = tmp_path / "bling_tokens.json"
    caminho.write_text("{quebrado", encoding="utf-8")
    with pytest.raises(ErroAutenticacao):
        TokenStore(caminho).carregar()
    assert caminho.read_text(encoding="utf-8") == "{quebrado"


def test_url_autorizacao_contem_client_id_e_state(credenciais, store):
    url = BlingAuth(store).url_autorizacao(state="abc")
    assert "response_type=code" in url
    assert "client_id=cid" in url
    assert "state=abc" in url


def test_url_autorizacao_sem_credenciais_orienta_operador(monkeypatch, store):
    monkeypatch.delenv("BLING_CLIENT_ID", raising=False)
    with pytest.raises(ErroAutenticacao, match="BLING_CLIENT_ID"):
        BlingAuth(store).url_autorizacao()


def test_trocar_codigo_persiste_tokens_normalizados(credenciais, store):
    sessao = Sessao([Resp(200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "token_type": "Bearer", "scope": "geral"})])
    auth = BlingAuth(store, session=sessao, relogio=lambda: 1000.0)
    tokens = auth.trocar_codigo("codigo-123")
    assert sessao.chamadas[0]["data"] == {"grant_type": "authorization_code", "code": "codigo-123"}
    assert tokens["expira_em"] == 4600
    assert store.carregar()["refresh_token"] == "r"
    assert tokens["escopo"] == "geral"


def test_access_token_renova_quando_falta_menos_de_60s(credenciais, store):
    store.salvar({"access_token": "velho", "refresh_token": "r1", "expira_em": 1030})
    sessao = Sessao([Resp(200, {"access_token": "novo", "refresh_token": "r2", "expires_in": 3600})])
    auth = BlingAuth(store, session=sessao, relogio=lambda: 1000.0)
    assert auth.access_token() == "novo"
    assert sessao.chamadas[0]["data"]["grant_type"] == "refresh_token"
    assert sessao.chamadas[0]["data"]["refresh_token"] == "r1"
    assert store.carregar()["refresh_token"] == "r2"


def test_refresh_recusado_preserva_os_tokens_anteriores(credenciais, store):
    store.salvar({"access_token": "velho", "refresh_token": "r1", "expira_em": 0})
    auth = BlingAuth(store, session=Sessao([Resp(400, {"error": "invalid_grant"})]), relogio=lambda: 1000.0)
    with pytest.raises(ErroAutenticacao) as exc:
        auth.access_token()
    assert "velho" not in str(exc.value) and "r1" not in str(exc.value)
    assert store.carregar()["refresh_token"] == "r1"


def test_sem_tokens_orienta_a_rodar_bling_auth(credenciais, store):
    with pytest.raises(ErroAutenticacao, match="bling-auth"):
        BlingAuth(store).access_token()


def test_access_token_usa_semente_do_env_quando_o_arquivo_nao_existe(credenciais, store, monkeypatch):
    monkeypatch.setenv("BLING_ACCESS_TOKEN", "semente")
    auth = BlingAuth(store, relogio=lambda: 1000.0)
    assert auth.access_token() == "semente"
    assert store.carregar() is None  # semear não grava arquivo
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_bling_auth.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wago.marketplaces.bling_auth'`.

- [ ] **Step 3: Implement `TokenStore` and `BlingAuth`**

- `TokenStore.__init__`: `self.caminho = Path(caminho) if caminho else STATE / "bling_tokens.json"` (importe `from wago.config import STATE` dentro do método para permitir monkeypatch de `wago.config.STATE`).
- `carregar()`: ausente → `None`; `json.JSONDecodeError`/`OSError` → `ErroAutenticacao(401, f"arquivo de tokens do Bling ilegível: {self.caminho}")` sem apagar nada.
- `salvar(tokens)`: exige `access_token` e `refresh_token` não vazios; acrescenta `atualizado_em` ISO 8601 (`datetime.now().isoformat(timespec="seconds")`); grava em `self.caminho.with_suffix(".tmp")` e `os.replace`; tenta `os.chmod(self.caminho, 0o600)` ignorando `OSError`.
- `BlingAuth.url_autorizacao`: exige `BLING_CLIENT_ID` (via `env`), senão `ErroAutenticacao(401, "BLING_CLIENT_ID ausente no .env")`; monta query com `response_type=code`, `client_id` e `state` quando houver; inclui `redirect_uri` quando `BLING_REDIRECT_URI` existir.
- `_pedir_token(dados)`: exige `BLING_CLIENT_ID`/`BLING_CLIENT_SECRET`; `session.post(URL_TOKEN, data=dados, auth=(client_id, client_secret), timeout=30)`; `requests.RequestException` ou status >= 400 → `ErroAutenticacao(status, mensagem do corpo, corpo)`. Mensagem nunca inclui tokens.
- `_normalizar(resposta)`: aceita envelope `{"data": {...}}`; devolve `{access_token, refresh_token, expira_em: int(relogio() + expires_in), token_type, escopo: scope, atualizado_em}`.
- `trocar_codigo`: `_pedir_token({"grant_type": "authorization_code", "code": code, **({"redirect_uri": ...} se configurado)})` → normaliza, `store.salvar` e devolve.
- `_tokens_do_env`: se `BLING_ACCESS_TOKEN` ou `BLING_REFRESH_TOKEN` existirem, devolve dict com `expira_em = int(relogio()) + 3600` quando houver access token, senão `0`.
- `access_token`: `tokens = store.carregar() or _tokens_do_env()`; sem tokens → `ErroAutenticacao(401, "sem tokens do Bling: rode `wago bling-auth --code <code>`")`; se `expira_em - relogio() < 60`, exige `refresh_token` e chama `renovar()`; devolve `access_token`.
- `renovar`: carrega tokens; sem `refresh_token` → `ErroAutenticacao`; `POST` com `grant_type=refresh_token` e o refresh atual; normaliza, salva e devolve. Falha não apaga o arquivo. Se a resposta de refresh não trouxer novo `refresh_token`, preserva o anterior em vez de chamar `salvar` sem ele.

- [ ] **Step 4: Run test to verify it passes**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_bling_auth.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/wago/marketplaces/bling_auth.py tests/test_bling_auth.py
git commit -m "feat(marketplaces): oauth e armazenamento de tokens do Bling"
```

---

### Task 5: `BlingClient` (HTTP, paginação e retry)

**Files:**
- Create: `src/wago/marketplaces/bling_provider.py`
- Test: `tests/test_bling_provider.py` (parte 1: cliente)

**Interfaces:**
- Consumes: `BlingAuth`, `RateLimiter`, `ErroMarketplace`, `ErroAutenticacao`, `ErroRateLimit`, `NaoEncontrado`.
- Produces:
  - `MAX_TENTATIVAS = 5`; `LIMITE_PAGINA = 100`.
  - `BlingClient(auth: BlingAuth, *, session=None, limiter: RateLimiter | None = None, timeout: int = 30, url_base: str = "https://www.bling.com.br/Api/v3")`.
  - `requisitar(metodo, caminho, *, params=None, json=None) -> Any`.
  - `paginar(caminho, params=None) -> Iterator[dict]`.
  - `_desembrulhar(corpo) -> Any` (staticmethod) e `_lista(corpo) -> list` (função de módulo).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bling_provider.py
import pytest

from wago.marketplaces.base import ErroAutenticacao, ErroRateLimit
from wago.marketplaces.bling_provider import BlingClient


class Resp:
    def __init__(self, status, corpo=None, headers=None):
        self.status_code = status
        self._corpo = corpo
        self.headers = headers or {}
        self.content = b"x" if corpo is not None else b""
        self.text = ""


class Sessao:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def request(self, metodo, url, headers=None, params=None, json=None, timeout=None):
        self.chamadas.append({"metodo": metodo, "url": url, "headers": headers, "params": params, "json": json})
        return self.respostas.pop(0)


class Auth:
    def __init__(self):
        self.renovacoes = 0

    def access_token(self):
        return "tok"

    def renovar(self):
        self.renovacoes += 1
        return {"access_token": "novo"}


class Limiter:
    def __init__(self):
        self.adquiridas = 0
        self.esperas = []

    def adquirir(self):
        self.adquiridas += 1

    def aguardar_429(self, headers, tentativa):
        self.esperas.append((dict(headers), tentativa))
        return 0.0


def _client(respostas):
    sessao, auth, limiter = Sessao(respostas), Auth(), Limiter()
    return BlingClient(auth, session=sessao, limiter=limiter), sessao, auth, limiter


def test_requisitar_inclui_bearer_e_desembrulha_data():
    client, sessao, _auth, _limiter = _client([Resp(200, {"data": [{"id": 1}]})])
    assert client.requisitar("GET", "/categorias/produtos") == [{"id": 1}]
    assert sessao.chamadas[0]["headers"]["Authorization"] == "Bearer tok"


def test_requisitar_renova_uma_vez_em_401():
    client, sessao, auth, _limiter = _client([Resp(401, {"error": "expired"}), Resp(200, {"data": []})])
    assert client.requisitar("GET", "/produtos") == []
    assert auth.renovacoes == 1
    assert len(sessao.chamadas) == 2


def test_requisitar_401_persistente_levanta_erro_de_autenticacao():
    client, _sessao, auth, _limiter = _client([Resp(401, {}), Resp(401, {})])
    with pytest.raises(ErroAutenticacao):
        client.requisitar("GET", "/produtos")
    assert auth.renovacoes == 1


def test_requisitar_429_respeita_retry_after_e_repete():
    client, sessao, _auth, limiter = _client([
        Resp(429, {}, {"Retry-After": "2"}),
        Resp(200, {"data": [1]}),
    ])
    assert client.requisitar("GET", "/produtos") == [1]
    assert limiter.esperas == [({"Retry-After": "2"}, 1)]
    assert len(sessao.chamadas) == 2


def test_requisitar_429_persistente_levanta_erro_rate_limit_apos_5_tentativas():
    client, sessao, _auth, _limiter = _client([Resp(429, {}) for _ in range(5)])
    with pytest.raises(ErroRateLimit):
        client.requisitar("GET", "/produtos")
    assert len(sessao.chamadas) == 5


def test_paginar_para_quando_pagina_vem_menor_que_o_limite():
    client, sessao, _auth, _limiter = _client([
        Resp(200, {"data": [{"id": i} for i in range(100)]}),
        Resp(200, {"data": [{"id": 100}]}),
    ])
    itens = list(client.paginar("/produtos"))
    assert len(itens) == 101
    assert sessao.chamadas[0]["params"] == {"pagina": 1, "limite": 100}
    assert sessao.chamadas[1]["params"] == {"pagina": 2, "limite": 100}


def test_paginar_trata_data_null_como_fim():
    client, _sessao, _auth, _limiter = _client([Resp(200, {"data": None})])
    assert list(client.paginar("/produtos")) == []


def test_requisitar_aceita_lista_direta_sem_envelope_data():
    client, _sessao, _auth, _limiter = _client([Resp(200, [{"id": 1}])])
    assert client.requisitar("GET", "/produtos") == [{"id": 1}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_bling_provider.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'wago.marketplaces.bling_provider'`.

- [ ] **Step 3: Implement `BlingClient`**

- `_desembrulhar`: `corpo["data"]` quando `isinstance(corpo, dict) and "data" in corpo`; senão `corpo`.
- `_lista`: `desembrulhado = _desembrulhar(corpo)`; devolve se for `list`, senão `[]`.
- `__init__`: `self.limiter = limiter or RateLimiter()`.
- `requisitar`: laço `for tentativa in range(1, MAX_TENTATIVAS + 1)`. Antes de cada request, `limiter.adquirir()`. Header `Authorization: Bearer {auth.access_token()}`, `Content-Type: application/json`, `Accept: application/json`. Status 401 na primeira ocorrência: `auth.renovar()` e repete **sem consumir tentativa** (controle `renovou`; use `while` se preferir). 429: se última tentativa → `ErroRateLimit(429, "limite de requisições do Bling esgotado", corpo)`; senão `limiter.aguardar_429(resp.headers, tentativa)` e continua. 5xx: mesmo backoff, `ErroMarketplace(status, "instabilidade do Bling", corpo)` na última. Corpo via `resp.json() if resp.content else None`, caindo em `resp.text` em `ValueError`. >=400 restantes → `_erro(status, corpo)`.
- `_erro(status, corpo)`: mensagem extraída de `corpo["error"]["message"]`, `corpo["error"]["description"]`, `corpo["error"]` (string), `corpo["message"]` ou `str(corpo)`; 401/403 → `ErroAutenticacao`, 404 → `NaoEncontrado`, 429 → `ErroRateLimit`, senão `ErroMarketplace`.
- `paginar`: `pagina = 1`; em laço, `itens = _lista(self.requisitar("GET", caminho, params={**(params or {}), "pagina": pagina, "limite": LIMITE_PAGINA}))`; se vazio, retorna; `yield from itens`; se `len(itens) < LIMITE_PAGINA`, retorna; `pagina += 1`.

- [ ] **Step 4: Run test to verify it passes**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_bling_provider.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/wago/marketplaces/bling_provider.py tests/test_bling_provider.py
git commit -m "feat(marketplaces): cliente HTTP do Bling com paginacao e retry"
```

---

### Task 6: `BlingProvider` (mapeamento de categorias e produtos)

**Files:**
- Modify: `src/wago/marketplaces/bling_provider.py`
- Test: `tests/test_bling_provider.py` (parte 2: provider, mesmo arquivo)

**Interfaces:**
- Consumes: `BlingClient`, `_lista`, `_desembrulhar`; contrato de `base.py`.
- Produces:
  - `BlingProvider(client: BlingClient)`; `nome = "bling"`.
  - `CAMPOS_PRODUTO_SO_LEITURA = frozenset({"id", "dataCriacao", "dataAlteracao", "dataExclusao"})`.
  - `_ids_produto(prod: dict) -> list[Id]`.

- [ ] **Step 1: Write the failing test (adicione os imports `CategoriaComProdutos`, `ErroMarketplace`, `NaoEncontrado`, `BlingProvider` e `_ids_produto` no topo de `tests/test_bling_provider.py`)**

```python
from wago.marketplaces.base import CategoriaComProdutos, ErroMarketplace, NaoEncontrado
from wago.marketplaces.bling_provider import BlingProvider, _ids_produto


class ClienteFake:
    def __init__(self, paginas=(), respostas=()):
        self.paginas = list(paginas)
        self.respostas = list(respostas)
        self.chamadas = []

    def paginar(self, caminho, params=None):
        self.chamadas.append(("GET", caminho, params, None))
        yield from self.paginas.pop(0)

    def requisitar(self, metodo, caminho, *, params=None, json=None):
        self.chamadas.append((metodo, caminho, params, json))
        resposta = self.respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


def test_listar_categorias_mapeia_nome_pai_e_descricao_vazia():
    client = ClienteFake(paginas=[[
        {"id": 1, "descricao": "Automação", "categoriaPaiId": None},
        {"id": 2, "descricao": "CLPs", "categoriaPaiId": 1},
    ]])
    assert BlingProvider(client).listar_categorias() == [
        {"id": 1, "nome": "Automação", "pai_id": None, "descricao": ""},
        {"id": 2, "nome": "CLPs", "pai_id": 1, "descricao": ""},
    ]


def test_listar_categorias_sem_produtos_devolve_lista_vazia():
    assert BlingProvider(ClienteFake(paginas=[[]])).listar_categorias() == []


def test_ids_produto_une_categoria_e_categorias_sem_duplicar():
    assert _ids_produto({"categoria": {"id": 5}, "categorias": [{"id": 5}, {"id": 6}]}) == [5, 6]
    assert _ids_produto({}) == []


def test_listar_produtos_mapeia_campos_ausentes_para_vazio():
    client = ClienteFake(paginas=[[
        {"id": 7, "nome": "Produto", "codigo": "SKU-1", "categoria": {"id": 5}},
        {"id": 8},
    ]])
    assert BlingProvider(client).listar_produtos() == [
        {"id": 7, "nome": "Produto", "sku": "SKU-1", "categorias": [5], "publicado": None},
        {"id": 8, "nome": "", "sku": "", "categorias": [], "publicado": None},
    ]


def test_criar_categoria_sem_pai_nao_envia_categoria_pai_id():
    client = ClienteFake(respostas=[{"id": 9, "descricao": "Nova"}])
    criada = BlingProvider(client).criar_categoria({"nome": "Nova", "pai_id": None, "descricao": ""})
    assert client.chamadas[0] == ("POST", "/categorias/produtos", None, {"descricao": "Nova"})
    assert criada["reutilizada"] is False and criada["nome"] == "Nova"


def test_criar_categoria_com_pai_envia_categoria_pai_id():
    client = ClienteFake(respostas=[{"id": 9, "descricao": "Filha", "categoriaPaiId": 1}])
    BlingProvider(client).criar_categoria({"nome": "Filha", "pai_id": 1, "descricao": ""})
    assert client.chamadas[0][3] == {"descricao": "Filha", "categoriaPaiId": 1}


def test_criar_categoria_recusa_descricao_diferente_do_nome():
    client = ClienteFake()
    with pytest.raises(ErroMarketplace) as exc:
        BlingProvider(client).criar_categoria({"nome": "Nova", "pai_id": None, "descricao": "Texto"})
    assert exc.value.status == 400 and client.chamadas == []


def test_atualizar_categoria_envia_somente_campos_alterados():
    client = ClienteFake(respostas=[{"id": 9, "descricao": "Renomeada"}])
    BlingProvider(client).atualizar_categoria(9, {"nome": "Renomeada"})
    assert client.chamadas[0] == ("PUT", "/categorias/produtos/9", None, {"descricao": "Renomeada"})


def test_atualizar_categoria_recusa_descricao():
    client = ClienteFake()
    with pytest.raises(ErroMarketplace) as exc:
        BlingProvider(client).atualizar_categoria(9, {"descricao": "x"})
    assert exc.value.status == 400 and client.chamadas == []


def test_deletar_categoria_bloqueia_quando_ha_produto_vinculado():
    client = ClienteFake(respostas=[[{"id": 7}]])
    with pytest.raises(CategoriaComProdutos):
        BlingProvider(client).deletar_categoria(5)
    assert all(chamada[0] != "DELETE" for chamada in client.chamadas)


def test_deletar_categoria_quando_filtro_recusado_tenta_delete():
    client = ClienteFake(respostas=[ErroMarketplace(400, "filtro inválido"), None])
    BlingProvider(client).deletar_categoria(5)
    assert client.chamadas[1][0] == "DELETE"


def test_deletar_categoria_inexistente_e_idempotente():
    client = ClienteFake(respostas=[[], NaoEncontrado(404, "não existe")])
    BlingProvider(client).deletar_categoria(5)  # não levanta


def test_atualizar_produto_uma_categoria_envia_categoria_singular_e_preserva_campos():
    atual = {"id": 7, "nome": "Antigo", "descricao": "Descrição", "categoria": {"id": 1}}
    client = ClienteFake(respostas=[atual, {"id": 7, **{k: v for k, v in atual.items() if k != "id"}, "nome": "Novo", "categoria": {"id": 2}}])
    produto = BlingProvider(client).atualizar_produto(7, {"categorias": [2], "nome": "Novo"})
    assert client.chamadas[0][0] == "GET" and client.chamadas[1][0] == "PUT"
    enviado = client.chamadas[1][3]
    assert enviado["nome"] == "Novo" and enviado["descricao"] == "Descrição"
    assert enviado["categoria"] == {"id": 2} and "categorias" not in enviado and "id" not in enviado
    assert produto["categorias"] == [2] and produto["publicado"] is None


def test_atualizar_produto_varias_categorias_envia_categorias_plural():
    client = ClienteFake(respostas=[{"id": 7, "nome": "P", "categoria": {"id": 1}}, {"id": 7, "nome": "P"}])
    BlingProvider(client).atualizar_produto(7, {"categorias": [2, 3]})
    enviado = client.chamadas[1][3]
    assert enviado["categorias"] == [{"id": 2}, {"id": 3}] and "categoria" not in enviado


def test_atualizar_produto_com_lista_vazia_remove_categorias():
    client = ClienteFake(respostas=[{"id": 7, "nome": "P", "categoria": {"id": 1}}, {"id": 7, "nome": "P"}])
    BlingProvider(client).atualizar_produto(7, {"categorias": []})
    enviado = client.chamadas[1][3]
    assert enviado["categorias"] == [] and "categoria" not in enviado


def test_atualizar_produto_recusa_publicado():
    client = ClienteFake()
    with pytest.raises(ErroMarketplace) as exc:
        BlingProvider(client).atualizar_produto(7, {"publicado": False})
    assert exc.value.status == 400 and client.chamadas == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_bling_provider.py -q`
Expected: FAIL with `ImportError: cannot import name 'BlingProvider'`.

- [ ] **Step 3: Implement `BlingProvider`**

- `_ids_produto(prod)`: começa com `categoria.id` (dict ou int) quando existir; depois percorre `categorias` (dicts ou ints); preserva a ordem e remove duplicados.
- `listar_categorias`: `[{"id": c.get("id"), "nome": str(c.get("descricao") or ""), "pai_id": c.get("categoriaPaiId") or None, "descricao": ""} for c in client.paginar("/categorias/produtos")]`.
- `criar_categoria`: se `dados.get("descricao")` não vazio e diferente de `dados["nome"]`, `ErroMarketplace(400, "Bling não separa descrição do nome da categoria")`. Corpo `{"descricao": dados["nome"]}` e `categoriaPaiId` só quando `dados.get("pai_id") is not None`. `POST /categorias/produtos`; mapeia a resposta com fallback para o corpo e acrescenta `reutilizada: False`.
- `atualizar_categoria`: se `"descricao" in dados` → `ErroMarketplace(400, "descricao de categoria não é suportada no Bling: use nome")`. Corpo só com `descricao` (de `nome`) e/ou `categoriaPaiId` (quando a chave `pai_id` existe). Sem campos → `ErroMarketplace(400, "nenhum campo para atualizar")`. `PUT /categorias/produtos/{id}`; mapeia com fallback para o corpo.
- `deletar_categoria`: `GET /produtos` com `params={"idCategoria": id, "pagina": 1, "limite": 1}`; se `_lista(resposta)` não vazia → `CategoriaComProdutos(409, f"categoria {id} tem produtos vinculados", resposta)`. `NaoEncontrado` ou `ErroMarketplace(400)` no GET → segue. `DELETE /categorias/produtos/{id}`; `NaoEncontrado` → retorna; `ErroMarketplace(400)` → `CategoriaComProdutos(409, "Bling recusou a exclusão: categoria com produtos", corpo)`.
- `listar_produtos`: `[{"id": p.get("id"), "nome": str(p.get("nome") or ""), "sku": str(p.get("codigo") or ""), "categorias": _ids_produto(p), "publicado": None} for p in client.paginar("/produtos")]`.
- `atualizar_produto`: `publicado` em `dados` → `ErroMarketplace(400, "publicado não é suportado no Bling")`. `GET /produtos/{id}`. Corpo = `{k: v for k, v in atual.items() if k not in CAMPOS_PRODUTO_SO_LEITURA}`; `nome` sobrescreve `nome`; `categorias` remove `categoria`/`categorias` antigos e grava `categoria: {"id": id}` para exatamente 1, `categorias: [{"id": ...}]` para 2+, ou `categorias: []` para lista vazia. `PUT /produtos/{id}`. Mapeia `{**atual, **corpo, **(resposta ou {})}` para tolerar resposta esparsa.

- [ ] **Step 4: Run test to verify it passes**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_bling_provider.py -q`
Expected: PASS (24 tests: 8 do cliente + 16 do provider).

- [ ] **Step 5: Commit**

```bash
git add src/wago/marketplaces/bling_provider.py tests/test_bling_provider.py
git commit -m "feat(marketplaces): provider Bling para categorias e produtos"
```

---

### Task 7: Factory e exports do pacote

**Files:**
- Modify: `src/wago/marketplaces/__init__.py`
- Test: `tests/test_marketplaces_base.py` (append)

**Interfaces:**
- Consumes: `NuvemshopProvider`, `BlingProvider`, `BlingClient`, `BlingAuth`, `RateLimiter`.
- Produces: `criar_provider(nome: str, **kwargs) -> BaseMarketplaceProvider`; exports públicos `BaseMarketplaceProvider`, erros, `NuvemshopProvider`, `BlingProvider`, `BlingClient`, `BlingAuth`, `TokenStore`, `RateLimiter`, `criar_provider`.

- [ ] **Step 1: Write the failing test**

```python
# append em tests/test_marketplaces_base.py
from wago.marketplaces import BlingProvider, NuvemshopProvider, criar_provider


class ApiNuvemshopFake:
    store_id = "123"
    def listar_categorias(self): return []
    def criar_categoria(self, nome, pai=None, **extra): return ({"id": 1}, False)
    def atualizar_categoria(self, cid, **campos): return {"id": cid}
    def excluir_categoria(self, cid): return None
    def listar_produtos(self): return []
    def obter_produto(self, pid): return {"id": pid}
    def atualizar_produto(self, pid, **campos): return {"id": pid}


def test_criar_provider_nuvemshop_aceita_api_injetada():
    provider = criar_provider("nuvemshop", api=ApiNuvemshopFake())
    assert isinstance(provider, NuvemshopProvider)


def test_criar_provider_bling_aceita_client_injetado():
    client = object()
    assert isinstance(criar_provider("bling", client=client), BlingProvider)


def test_criar_provider_desconhecido_lista_os_validos():
    with pytest.raises(ValueError, match="nuvemshop.*bling"):
        criar_provider("shopee")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_marketplaces_base.py -q`
Expected: FAIL with `ImportError: cannot import name 'criar_provider'`.

- [ ] **Step 3: Implement `criar_provider` e exports**

- `criar_provider("nuvemshop", **kwargs)` → `NuvemshopProvider(**kwargs)`.
- `criar_provider("bling", client=None, **kwargs)` → `BlingProvider(client or BlingClient(BlingAuth(**kwargs)))`.
- Nome desconhecido → `ValueError(f"marketplace desconhecido: {nome!r}; use 'nuvemshop' ou 'bling'")`.
- `__all__` com todos os nomes públicos da Interface.

- [ ] **Step 4: Run test to verify it passes**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_marketplaces_base.py -q`
Expected: PASS (10 tests: 7 anteriores + 3 da factory).

- [ ] **Step 5: Commit**

```bash
git add src/wago/marketplaces/__init__.py tests/test_marketplaces_base.py
git commit -m "feat(marketplaces): factory criar_provider e exports"
```

---

### Task 8: CLI `bling-auth` / `bling-categorias` e `.env.example`

**Files:**
- Modify: `src/wago/__main__.py` (imports/parser perto de `nuvemshop-auth`, dispatch perto de `taxonomia`)
- Modify: `.env.example`
- Test: `tests/test_marketplaces_cli.py`

**Interfaces:**
- Consumes: `BlingAuth`, `criar_provider`, `ErroMarketplace`/`ErroAutenticacao`.
- Produces: comandos `wago bling-auth --code <code>` e `wago bling-categorias`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_marketplaces_cli.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_marketplaces_cli.py -q`
Expected: FAIL with `SystemExit: 2` (comando inexistente no parser).

- [ ] **Step 3: Implement the CLI and env entries**

- No parser, depois de `nuvemshop-auth`:
  - `bling-auth` com `--code` obrigatório.
  - `bling-categorias` sem argumentos.
- No dispatch, antes de `taxonomia`:
  - `bling-auth`: `from wago.marketplaces.bling_auth import BlingAuth`; `tokens = BlingAuth().trocar_codigo(args.code)`; imprime `Token do Bling salvo; válido até {datetime.fromtimestamp(tokens["expira_em"]).isoformat(timespec="seconds")}`. Captura `ErroAutenticacao`/`ErroMarketplace` e `raise SystemExit(f"Falha: {exc}")` (a mensagem não contém tokens).
  - `bling-categorias`: `from wago.marketplaces import criar_provider`; `cats = criar_provider("bling").listar_categorias()`; imprime `f"{c['id']}\t{c['nome']}"` e `\tpai={c['pai_id']}` quando não nulo; termina com `f"{len(cats)} categoria(s)."`. Mesma tradução de erro.
- Em `.env.example`, após o bloco da Nuvemshop:

```dotenv
# Bling ERP (API v3) — `wago bling-auth --code <code>` e `wago bling-categorias`
BLING_CLIENT_ID=
BLING_CLIENT_SECRET=
BLING_REDIRECT_URI=
BLING_ACCESS_TOKEN=
BLING_REFRESH_TOKEN=
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& ".venv\Scripts\python.exe" -m pytest tests/test_marketplaces_cli.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/wago/__main__.py .env.example tests/test_marketplaces_cli.py
git commit -m "feat(cli): bling-auth e bling-categorias"
```

---

### Task 9: Verificação final e fumaça manual

**Files:**
- No code changes expected; fix only regressions found.
- Test: suite completa.

**Interfaces:**
- Consumes: tudo das tarefas anteriores.
- Produces: confirmação de que o Bling provider está pronto para receber a taxonomia v8.

- [ ] **Step 1: Run the full suite**

Run: `& ".venv\Scripts\python.exe" -m pytest -q`
Expected: PASS, sem falhas; a contagem deve ser a anterior (206) mais os testes novos (10 + 5 + 12 + 9 + 24 + 5 = 65), total 271.

- [ ] **Step 2: Confirm the CLI help works**

Run: `& ".venv\Scripts\python.exe" -m wago bling-auth --help`
Expected: mostra `--code`.

Run: `& ".venv\Scripts\python.exe" -m wago bling-categorias --help`
Expected: mostra o comando sem exigir credenciais.

- [ ] **Step 3: Confirm no legacy files changed**

Run: `git status --short` e `git diff --stat HEAD~8`
Expected: só arquivos novos em `src/wago/marketplaces/`, `tests/test_marketplaces_*.py`, `tests/test_bling_*.py`, mais `src/wago/__main__.py` e `.env.example`. Nenhuma diferença em `src/wago/taxonomia/`.

- [ ] **Step 4: Manual smoke (operator, real credentials)**

1. Preencher `BLING_CLIENT_ID`, `BLING_CLIENT_SECRET` e `BLING_REDIRECT_URI` no `.env`.
2. Gerar a URL com `& ".venv\Scripts\python.exe" -c "from wago.marketplaces import BlingAuth; print(BlingAuth().url_autorizacao())"`, abri-la no navegador e capturar o `code` do redirecionamento.
3. Rodar `& ".venv\Scripts\python.exe" -m wago bling-auth --code <code>`.
4. Rodar `& ".venv\Scripts\python.exe" -m wago bling-categorias` e confirmar que as categorias reais aparecem.
5. Se a API real divergir do envelope/`idCategoria`/`categoria` vs `categorias`, ajustar apenas `bling_provider.py` e os mocks correspondentes.

- [ ] **Step 5: Commit any fixes**

```bash
git add src/wago tests docs/superpowers .env.example
git commit -m "test(marketplaces): verificacao final do provider Bling"
```
