"""Contrato canônico de marketplaces: modelos, erros e interface base.

Os TypedDicts são dicionários JSON-friendly, alinhados ao diário do motor.
Cada adapter traduz os erros da sua API para os erros canônicos definidos
aqui; os módulos existentes de `taxonomia/` não mudam.
"""

from abc import ABC, abstractmethod
from typing import Any, NotRequired, TypedDict

Id = str | int


class Categoria(TypedDict):
    id: Id
    nome: str  # texto simples, não i18n
    pai_id: Id | None
    descricao: str  # "" quando o marketplace não separa descrição do nome
    reutilizada: NotRequired[bool]  # só no retorno de criar_categoria


class NovaCategoria(TypedDict):
    nome: str
    pai_id: NotRequired[Id | None]  # opcional
    descricao: NotRequired[str]  # opcional; vazio = não enviar


class AlteracaoCategoria(TypedDict, total=False):
    nome: str
    pai_id: Id | None
    descricao: str


class Produto(TypedDict):
    id: Id
    nome: str
    sku: str
    categorias: list[Id]  # ids de categoria
    publicado: bool | None  # disponível na vitrine; None = conceito inexistente


class AlteracaoProduto(TypedDict, total=False):
    categorias: list[Id]
    publicado: bool
    nome: str


class ErroMarketplace(Exception):
    """Erro canônico de API de marketplace: status HTTP, mensagem e corpo."""

    def __init__(self, status: int, mensagem: str, corpo: Any = None) -> None:
        super().__init__(f"HTTP {status}: {mensagem}")
        self.status = status
        self.mensagem = mensagem
        self.corpo = corpo


class ErroAutenticacao(ErroMarketplace):
    """401/403 e falha de OAuth."""


class ErroRateLimit(ErroMarketplace):
    """Cota diária esgotada ou 429 persistente."""


class CategoriaComProdutos(ErroMarketplace):
    """Exclusão de categoria bloqueada por vínculo com produtos."""


class NaoEncontrado(ErroMarketplace):
    """Recurso não existe (404)."""


class BaseMarketplaceProvider(ABC):
    """Interface que todo provider de marketplace implementa."""

    nome: str = ""

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
