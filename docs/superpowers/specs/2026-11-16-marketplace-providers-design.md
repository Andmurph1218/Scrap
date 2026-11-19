# Motor de categorias agnóstico ao marketplace — design

**Data:** 2026-11-16
**Status:** proposto (aguardando aprovação)
**Escopo aprovado:** A — camada de providers + adapter Nuvemshop + Bling (auth, cliente e provider) com testes. O motor de aplicação continua Nuvemshop-only nesta entrega.

## Intenção e porquê

O Motor de Categorias hoje só aplica a taxonomia na Nuvemshop. O objetivo é destravar a
mesma taxonomia (v8) para o Bling ERP sem duplicar lógica de categorias/produtos: uma
interface comum, um adapter para a Nuvemshop que preserve o comportamento atual e um
provider novo para o Bling v3.

O fluxo Nuvemshop que já funciona (aplicar, retomar, reverter, inventário) **não muda
nesta entrega**. A abstração é aditiva: `taxonomia/nuvemshop_api.py` e `aplicar.py`
continuam sendo usados como hoje. Isso protege o que está em produção e deixa a
neutralização do motor (escopo B) para uma fase validada contra a API real.

## Objetivos

1. Definir `BaseMarketplaceProvider` com o contrato canônico de categorias e produtos.
2. Implementar `NuvemshopProvider` como adapter sobre o `NuvemshopAPI` existente, com
   paridade de chamadas e payloads.
3. Implementar `BlingAuth` (OAuth 2.0, authorization_code + refresh_token) com
   persistência segura dos tokens.
4. Implementar `BlingProvider` (categorias e produtos, paginação, rate limit, retry).
5. Cobrir mapeamento, paginação, auth e rate limit com testes unitários e mocks,
   sem tocar na API real.
6. Deixar o provider do Bling pronto para receber a taxonomia v8: listar, criar,
   renomear, reparentar, excluir categoria e vincular produto a categoria.

## Não-objetivos

- Neutralizar `aplicar.py`, diário, retomada e reversão (escopo B).
- Criar produtos no Bling.
- Sincronizar estoque, preço, NF-e ou qualquer outro domínio do ERP.
- Servidor OAuth interativo / callback automático.
- Async. O projeto é síncrono (`requests`); o rate limiter também será.

## Anti-objetivos

- Quebrar o fluxo Nuvemshop atual ou exigir migração de dados dele.
- Escrever no Bling sem o operador ter feito o login OAuth explicitamente.
- Silenciar erros de API: falha de mapeamento deve aparecer com o corpo da resposta.
- Guardar segredo em log, commit ou mensagem de erro.

## Restrições

- Python 3.11+; dependências atuais apenas (`requests`, `python-dotenv`). Não há
  `cryptography` instalada.
- Bling v3: 3 requisições/s e 120.000/dia por conta; HTTP 429 precisa ser tratado.
- A documentação do Bling é um app JS e não expõe OpenAPI público; o formato exato de
  alguns campos não pôde ser verificado nesta máquina. O mapeamento fica isolado para
  correção barata no primeiro teste real.
- `data/state/*` já é ignorado pelo Git — é o lugar natural para os tokens.

## Abordagens consideradas

### A — Providers aditivos (escolhida)

Novo pacote `wago/marketplaces/` com interface, adapter Nuvemshop e provider Bling. O
motor existente continua no `NuvemshopAPI`. Menor risco, entrega o Bling funcional e
prepara o escopo B sem reescrever o que já foi validado em produção.

Trade-off: por um tempo existem dois caminhos para a Nuvemshop (o antigo e o adapter).
O adapter é a referência para a migração futura.

### B — Neutralizar o motor agora

Trocar as ações/diário/reversão por um modelo agnóstico e aplicar a v8 no Bling de
ponta a ponta. Entrega mais completa, porém mexe em ~600 linhas com diário, retomada e
reversão já validados; exige validação contra a API real antes de confiar. Fica para a
fase seguinte, com o provider desta entrega como base.

### C — Só cliente HTTP, sem interface

Criar `BlingAPI` isolado e deixar a abstração para depois. Mais rápido, mas não atende
ao requisito de arquitetura e não cria o seam necessário para o escopo B. Descartada.

## Desenho

### Pacote

```
src/wago/marketplaces/
├── __init__.py          # exports + criar_provider()
├── base.py              # BaseMarketplaceProvider, modelos canônicos e erros
├── nuvemshop_provider.py# NuvemshopProvider (adapter sobre NuvemshopAPI)
├── rate_limit.py        # RateLimiter (3/s, 120k/dia)
├── bling_auth.py        # BlingAuth + TokenStore
└── bling_provider.py    # BlingClient (HTTP) + BlingProvider (mapeamento)
```

Testes: `tests/test_marketplaces_base.py`, `tests/test_nuvemshop_provider.py`,
`tests/test_bling_auth.py`, `tests/test_bling_rate_limit.py`,
`tests/test_bling_provider.py`.

### Contrato canônico

`base.py` define `TypedDict`s (dicionários JSON-friendly, alinhados ao diário atual):

```python
Id = str | int

class Categoria(TypedDict):
    id: Id
    nome: str                 # texto simples, não i18n
    pai_id: Id | None
    descricao: str            # "" quando o marketplace não separa descrição do nome

class NovaCategoria(TypedDict):
    nome: str
    pai_id: Id | None         # opcional
    descricao: str            # opcional; vazio = não enviar

class AlteracaoCategoria(TypedDict, total=False):
    nome: str
    pai_id: Id | None
    descricao: str

class Produto(TypedDict):
    id: Id
    nome: str
    sku: str
    categorias: list[Id]      # ids de categoria
    publicado: bool | None    # disponível na vitrine; None = conceito inexistente

class AlteracaoProduto(TypedDict, total=False):
    categorias: list[Id]
    publicado: bool
    nome: str
```

Interface:

```python
class BaseMarketplaceProvider(ABC):
    nome: str

    @abstractmethod
    def listar_categorias(self) -> list[Categoria]: ...
    @abstractmethod
    def criar_categoria(self, dados: NovaCategoria) -> Categoria: ...
    @abstractmethod
    def atualizar_categoria(self, id: Id, dados: AlteracaoCategoria) -> Categoria: ...
    @abstractmethod
    def deletar_categoria(self, id: Id) -> None: ...
    @abstractmethod
    def listar_produtos(self) -> list[Produto]: ...
    @abstractmethod
    def atualizar_produto(self, id: Id, dados: AlteracaoProduto) -> Produto: ...
```

`criar_categoria` devolve a categoria com a chave extra `reutilizada: bool` (a Nuvemshop
reaproveita categoria existente em conflito de nome; o Bling não).

Erros canônicos em `base.py`:

- `ErroMarketplace(status, mensagem, corpo)` — base.
- `ErroAutenticacao(ErroMarketplace)` — 401/403 e falha de OAuth.
- `ErroRateLimit(ErroMarketplace)` — cota diária esgotada ou 429 persistente.
- `CategoriaComProdutos(ErroMarketplace)` — exclusão bloqueada por vínculo.
- `NaoEncontrado(ErroMarketplace)` — 404.

Cada adapter traduz os erros da sua API para esses tipos; os módulos antigos não mudam.

### NuvemshopProvider

Composição, não herança: recebe um `NuvemshopAPI` (ou cria via `do_env()`).

| Canônico | Nuvemshop |
|---|---|
| `nome` | `texto_i18n(name)` |
| `pai_id` | `parent` |
| `descricao` | `texto_i18n(description)` |
| `categorias` (produto) | `categories` |
| `publicado` | `visibility != "hidden"` e `published != False`; `published` sozinho é fallback legado |
| `sku` (produto) | primeira variante com `sku` |

- `criar_categoria` → `criar_categoria(nome, pai)`; devolve `reutilizada`.
- `atualizar_categoria` → `atualizar_categoria(id, name={"pt": ...}, parent=...)`; a
  mesclagem de idiomas e o merge do estado atual continuam no `NuvemshopAPI`.
- `deletar_categoria` → `excluir_categoria` (404 idempotente).
- `atualizar_produto` → `categories=`; `publicado=False` envia `visibility="hidden"` e,
  em 422, cai para `published=False` (mesmo caminho de `_executar`/`arquivar_produto`
  em produção); `publicado=True` envia `visibility="visible"`.
  `nome` faz GET antes do PUT e mescla o `name` i18n para não apagar `es`/`en`.
  `sku` não é atualizável no nível do produto → `ErroMarketplace(400, ...)`.

Teste de paridade: um `FakeSession` que registra método/URL/payload confirma que o
adapter produz as mesmas chamadas que o fluxo atual.

### BlingAuth e tokens

`bling_auth.py`:

```python
class TokenStore:
    def carregar(self) -> dict | None
    def salvar(self, tokens: dict) -> None   # atômico: .tmp + os.replace; chmod 0600

class BlingAuth:
    def url_autorizacao(self, state: str | None = None) -> str
    def trocar_codigo(self, code: str) -> dict      # authorization_code
    def access_token(self) -> str                   # renova se faltar < 60s
    def renovar(self) -> dict                       # refresh_token (rotativo)
```

- Endpoint: `POST https://www.bling.com.br/Api/v3/oauth/token`, `Content-Type:
  application/x-www-form-urlencoded`, Basic auth `client_id:client_secret`.
- Autorização: `https://www.bling.com.br/Api/v3/oauth/authorize?response_type=code&client_id=...&state=...`
  (o OAuth continua em `www`; a API de dados mudou para `api.bling.com.br`).
- `url_autorizacao` gera um `state` aleatório quando não recebe um: apps com a
  validação de `state` ligada recusam a URL sem ele. O fluxo é manual (`--code`),
  então o `state` não é conferido no retorno.
- Campos persistidos: `access_token`, `refresh_token`, `expira_em` (epoch),
  `token_type`, `escopo`, `atualizado_em`.
- Arquivo: `data/state/bling_tokens.json` (já ignorado pelo Git). Leitura aceita
  `BLING_ACCESS_TOKEN`/`BLING_REFRESH_TOKEN` do `.env` como semente quando o arquivo
  não existe; a renovação sempre grava no arquivo.
- **Decisão:** JSON com permissão restrita, não criptografado. Não há `cryptography`
  no projeto e a chave teria que morar no mesmo host/`.env`, o que não agrega proteção
  real. Fica registrado como ponto de endurecimento futuro (cofre do SO ou KMS).

`BLING_*` no `.env.example`: `BLING_CLIENT_ID`, `BLING_CLIENT_SECRET`,
`BLING_REDIRECT_URI` (opcional), `BLING_ACCESS_TOKEN`, `BLING_REFRESH_TOKEN` (sementes).

### Rate limiter

`RateLimiter(por_segundo=3, por_dia=120_000)`, síncrono:

- Espaçamento mínimo de `1/3 s` entre requisições (`0.34`), relógio e `sleep`
  injetáveis para teste.
- Janela diária deslizante de 24h desde a primeira requisição do processo; ao estourar,
  levanta `ErroRateLimit` (não dá para esperar 24h dentro de um job).
- Em HTTP 429: respeita `Retry-After` quando presente; senão backoff `0.34 * 2^n`,
  até 5 tentativas; 5xx seguem o mesmo backoff.
- Limite é por processo. Dois jobs simultâneos podem somar; documentado como
  responsabilidade do operador (o worker do dashboard já serializa a fila).

### BlingProvider

`BlingClient` (HTTP) + `BlingProvider` (mapeamento), no mesmo módulo.

Cliente:
- Base `https://api.bling.com.br/Api/v3`. O host `www.bling.com.br` passou a
  responder **403** para chamadas de API ("endpoint oficial: api.bling.com.br").
- Header `Authorization: Bearer <access_token>`; em 401 renova uma vez e repete.
- Paginação por `pagina` (1-based) e `limite` (padrão 100), parando quando vier menos
  que o limite. Respostas podem vir como `{"data": ...}` ou direto; um `_desembrulhar`
  cobre os dois.

Mapeamento:

| Canônico | Bling v3 |
|---|---|
| `nome` | `descricao` |
| `pai_id` (leitura) | `categoriaPai.id` (0 = raiz) — formato real da API |
| `pai_id` (escrita) | `categoriaPai: {"id": N}`; `categoriaPaiId` é **ignorado** |
| `sku` | `codigo` |
| `categorias` | `categoria.id` / `categorias[].id` (só no GET individual) |
| `publicado` | sem equivalente confiável → `None` |

**Descobertas do smoke real** (verificado com credenciais): `POST` aplica o pai
somente com `categoriaPai: {"id": N}` (responde 201 e ignora `categoriaPaiId`);
`PUT` exige `descricao` e **não move a categoria** (aceita o corpo, responde 204 e
mantém o pai) — o provider recusa `pai_id` em `atualizar_categoria` com
`ErroMarketplace(400)` em vez de no-op silencioso. A resposta do `POST`/`PUT` é
esparsa; o provider confirma por `GET`.

Na leitura de categoria, `descricao` é sempre `""`: no Bling a `descricao` **é** o nome
da categoria. Enviar `descricao` de categoria levanta `ErroMarketplace(400, "não
suportado")` para nunca renomear por acidente. `AlteracaoProduto.nome` vai para
`nome` do produto, que existe no Bling.

- `listar_categorias` → `GET /categorias/produtos`. A "Categoria padrão" do Bling
  (id variável por conta) **não** é retornada pela API; produtos nela aparecem
  como não classificados.
- `criar_categoria` → `POST /categorias/produtos` com `descricao` e
  `categoriaPai: {"id": ...}` só quando houver pai; confirma o estado por `GET`.
- `atualizar_categoria` → `GET` do estado atual, `PUT` com `descricao` (obrigatória
  na API) e confirmação por `GET`; recusa `pai_id` porque a API não move categoria.
- `deletar_categoria` → antes do `DELETE`, verifica vínculo com
  `GET /produtos?idCategoria={id}&limite=1`; se houver produto, levanta
  `CategoriaComProdutos`. Se a API recusar o filtro (400), segue para o `DELETE` e
  traduz a recusa. 404 é idempotente.
- `listar_produtos` → `GET /produtos` para os dados e, como a listagem não traz
  categoria, um mapa produto→categorias via `GET /produtos?idCategoria={id}` por
  categoria (~1 + N chamadas, sem N+1 por produto). Produtos na "Categoria padrão"
  ficam com `categorias: []`.
- `atualizar_produto` → `GET /produtos/{id}` para mesclar (lição do PUT da Nuvemshop:
  omitir campo apaga), aplica só o que veio no canônico e envia `PUT`.
  Uma categoria → `categoria: {"id": ...}`; várias → `categorias: [{"id": ...}, ...]`.
  `publicado` no Bling levanta `ErroMarketplace(400, "não suportado")` em vez de
  fingir que aplicou. `nome` vai para `nome`.

### Factory e CLI

- `criar_provider(nome)` aceita `"nuvemshop"` e `"bling"`; erro claro para nome
  desconhecido.
- `wago bling-auth --code <code>`: troca o code, grava os tokens e imprime validade
  (sem exibir os tokens).
- `wago bling-categorias`: lista categorias via provider (read-only), para fumaça
  contra a API real.

### Tratamento de falhas

- Nenhum token em log, exceção ou arquivo de diário.
- Erros de API carregam `status`, mensagem e corpo (truncado) para diagnóstico.
- Falha de refresh não apaga o token anterior; o operador refaz o authorization_code.

## Decisões do autor (revisáveis)

1. Pacote novo e aditivo em vez de reescrever `aplicar.py` (escopo A).
2. Dicionários canônicos (`TypedDict`) em vez de dataclasses, para casar com o diário
   JSON existente.
3. Tokens em JSON com permissão restrita; sem criptografia nesta fase.
4. Rate limiter síncrono, por processo.
5. `publicado` não suportado no Bling: erro explícito.
6. `atualizar_produto` faz GET antes do PUT para não apagar campos.
7. `publicado` canônico = "disponível na vitrine". Na Nuvemshop deriva de `visibility`
   (`hidden` → `False`) com `published` como fallback legado; escrever `False` usa
   `visibility` e cai para `published` só em 422, igual ao fluxo de produção.
8. Campo sem equivalente falha explícito em vez de ser ignorado: `publicado` no Bling
   e `descricao` de categoria no Bling levantam `ErroMarketplace(400)`.
9. Renomear produto na Nuvemshop faz GET + merge do `name` i18n, preservando `es`/`en`.

## O que fica com o construtor (incertezas da API)

- Formato do envelope de resposta do Bling (`data` vs objeto direto) — tratado de forma
  defensiva nos dois formatos.
- Nome do parâmetro de filtro de produtos por categoria (`idCategoria`) e o
  comportamento do `DELETE` com vínculo — teste real ajusta.
- `categoria` singular vs `categorias` plural no PUT de produto — implementado conforme
  o enunciado; ajuste barato no mapeamento.
- Se o Bling exige `redirect_uri` no token — parâmetro opcional.

## Critérios de aceite

1. `pytest` verde (suite atual + testes novos).
2. `NuvemshopProvider` produz as mesmas chamadas HTTP que o fluxo atual nos cenários
   cobertos, incluindo arquivamento (`visibility` + fallback 422) e rename de produto
   com merge de idiomas.
3. `BlingProvider` passa nos testes com JSONs mockados de categorias e produtos,
   incluindo paginação, criação com/sem pai, atualização, exclusão bloqueada e vínculo
   de produto.
4. `BlingAuth` renova token expirado, persiste o refresh rotativo e não vaza segredo.
5. Rate limiter respeita 3/s e 120k/dia e reage a 429.
6. `wago bling-categorias` lista categorias com credenciais reais (verificação manual
   do operador).
7. Nenhuma mudança de comportamento no fluxo Nuvemshop atual.
