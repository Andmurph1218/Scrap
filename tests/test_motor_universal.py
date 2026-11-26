"""Testes para o motor universal de taxonomia (IDResolver, plano_validator, etc.)."""

import json
import pytest
from pathlib import Path
from wago.taxonomia.id_resolver import IDResolver
from wago.taxonomia.plano_validator import validar_plano
from wago.taxonomia.motor_universal import (
    DiarioAgnostico,
    AplicadorUniversal,
    reverter_universal,
    carregar_diario_agnostico,
)
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
from typing import Any


# Fixtures com dados de exemplo
@pytest.fixture
def taxonomia_v8_exemplo():
    """Taxonomia v8 de exemplo com nós para Nuvemshop e Bling."""
    return {
        "versao": 8,
        "nos": [
            {"id_alvo": "cat1", "nome": "Automação", "pai": None, "nuvemshop_id": 100, "bling_id": 10},
            {"id_alvo": "cat2", "nome": "CLPs", "pai": "cat1", "nuvemshop_id": 200, "bling_id": 20},
            {"id_alvo": "cat3", "nome": "Sensores", "pai": "cat1", "nuvemshop_id": None, "bling_id": None},  # nova
            {"id_alvo": "cat4", "nome": "Comutação", "pai": None, "nuvemshop_id": 300, "bling_id": 30},
        ]
    }


@pytest.fixture
def bling_mapping_exemplo():
    """Mapeamento id_alvo → bling_id."""
    return {
        "cat1": 10,
        "cat2": 20,
        "cat4": 30,
    }


@pytest.fixture
def diario_refs_exemplo():
    """Referências do diário: 'novo:id_alvo' → provider_id."""
    return {
        "novo:cat3": 40,  # categoria criada no Bling
    }


class TestIDResolver:
    """Testes para o IDResolver."""

    def test_resolver_categoria_nuvemshop_id_existente(self, taxonomia_v8_exemplo):
        """Deve resolver nuvemshop_id para categorias existentes na Nuvemshop."""
        resolver = IDResolver(
            provider_nome="nuvemshop",
            taxonomia_v8=taxonomia_v8_exemplo,
            bling_mapping={},
            diario_refs={}
        )
        assert resolver.resolver_categoria("cat1") == 100
        assert resolver.resolver_categoria("cat2") == 200
        assert resolver.resolver_categoria("cat4") == 300

    def test_resolver_categoria_nuvemshop_erro_sem_id(self, taxonomia_v8_exemplo):
        """Deve levantar ValueError se categoria não tem nuvemshop_id."""
        resolver = IDResolver(
            provider_nome="nuvemshop",
            taxonomia_v8=taxonomia_v8_exemplo,
            bling_mapping={},
            diario_refs={}
        )
        with pytest.raises(ValueError, match="nuvemshop_id"):
            resolver.resolver_categoria("cat3")

    def test_resolver_categoria_bling_id_existente(self, taxonomia_v8_exemplo, bling_mapping_exemplo):
        """Deve resolver bling_id via bling_mapping para o Bling."""
        resolver = IDResolver(
            provider_nome="bling",
            taxonomia_v8=taxonomia_v8_exemplo,
            bling_mapping=bling_mapping_exemplo,
            diario_refs={}
        )
        assert resolver.resolver_categoria("cat1") == 10
        assert resolver.resolver_categoria("cat2") == 20
        assert resolver.resolver_categoria("cat4") == 30

    def test_resolver_categoria_bling_erro_sem_mapping(self, taxonomia_v8_exemplo, bling_mapping_exemplo):
        """Deve levantar ValueError se id_alvo não está no bling_mapping."""
        resolver = IDResolver(
            provider_nome="bling",
            taxonomia_v8=taxonomia_v8_exemplo,
            bling_mapping=bling_mapping_exemplo,
            diario_refs={}
        )
        with pytest.raises(ValueError, match="bling_mapping"):
            resolver.resolver_categoria("cat3")

    def test_resolver_categoria_novo_ref_diario(self, taxonomia_v8_exemplo, diario_refs_exemplo):
        """Deve resolver referências 'novo:...' via diario_refs."""
        resolver = IDResolver(
            provider_nome="bling",
            taxonomia_v8=taxonomia_v8_exemplo,
            bling_mapping={},
            diario_refs=diario_refs_exemplo
        )
        assert resolver.resolver_categoria("novo:cat3") == 40

    def test_resolver_categoria_novo_ref_erro_se_ausente(self, taxonomia_v8_exemplo):
        """Deve levantar ValueError se referência 'novo:...' não está no diário."""
        resolver = IDResolver(
            provider_nome="bling",
            taxonomia_v8=taxonomia_v8_exemplo,
            bling_mapping={},
            diario_refs={}
        )
        with pytest.raises(ValueError, match="diario_refs"):
            resolver.resolver_categoria("novo:cat3")

    def test_resolver_acao_traduz_campos(self, taxonomia_v8_exemplo, bling_mapping_exemplo):
        """resolver_acao deve traduzir campos categoria, pai, para usando resolver_categoria."""
        resolver = IDResolver(
            provider_nome="bling",
            taxonomia_v8=taxonomia_v8_exemplo,
            bling_mapping=bling_mapping_exemplo,
            diario_refs={}
        )
        acao = {
            "acao": "reparentar",
            "categoria": "cat2",
            "pai": "cat1",
            "para": "cat4",
        }
        resultado = resolver.resolver_acao(acao)
        assert resultado["categoria"] == 20
        assert resultado["pai"] == 10
        assert resultado["para"] == 30
        # Outros campos preservados
        assert resultado["acao"] == "reparentar"

    def test_resolver_acao_com_ref_novo(self, taxonomia_v8_exemplo, diario_refs_exemplo):
        """resolver_acao deve resolver referências 'novo:...' nos campos."""
        resolver = IDResolver(
            provider_nome="bling",
            taxonomia_v8=taxonomia_v8_exemplo,
            bling_mapping={},
            diario_refs=diario_refs_exemplo
        )
        acao = {
            "acao": "criar",
            "nome": "Nova Categoria",
            "pai": "novo:cat3",
        }
        resultado = resolver.resolver_acao(acao)
        assert resultado["pai"] == 40

    def test_resolver_acao_preserva_campos_nao_ids(self, taxonomia_v8_exemplo, bling_mapping_exemplo):
        """Campos que não são IDs de categoria devem ser preservados."""
        resolver = IDResolver(
            provider_nome="bling",
            taxonomia_v8=taxonomia_v8_exemplo,
            bling_mapping=bling_mapping_exemplo,
            diario_refs={}
        )
        acao = {
            "acao": "mover_produto",
            "produto": 123,
            "nome": "Produto Teste",
            "de": ["cat1"],
            "para": ["cat2"],
            "origem": "humano",
        }
        resultado = resolver.resolver_acao(acao)
        assert resultado["produto"] == 123
        assert resultado["nome"] == "Produto Teste"
        assert resultado["origem"] == "humano"
        assert resultado["de"] == [10]
        assert resultado["para"] == [20]


class TestPlanoValidator:
    """Testes para o validador de plano."""

    def test_validar_plano_nuvemshop_aceita_reparentar(self):
        """Nuvemshop deve aceitar ações de reparentar."""
        plano = {
            "fases": {
                "criar": [],
                "renomear": [],
                "reparentar": [{"acao": "reparentar", "categoria": 1, "para": 2}],
                "mover": [],
                "arquivar": [],
                "verificar": [],
                "excluir": [],
            }
        }
        # Não deve levantar exceção
        validar_plano(plano, "nuvemshop")

    def test_validar_plano_bling_rejeita_reparentar(self):
        """Bling deve rejeitar planos com ações de reparentar."""
        plano = {
            "fases": {
                "criar": [],
                "renomear": [],
                "reparentar": [{"acao": "reparentar", "categoria": 1, "para": 2}],
                "mover": [],
                "arquivar": [],
                "verificar": [],
                "excluir": [],
            }
        }
        with pytest.raises(ValueError, match="Bling não suporta reparentar"):
            validar_plano(plano, "bling")

    def test_validar_plano_bling_aceita_sem_reparentar(self):
        """Bling deve aceitar planos sem reparentar."""
        plano = {
            "fases": {
                "criar": [{"acao": "criar", "nome": "Nova"}],
                "renomear": [{"acao": "renomear", "categoria": 1, "nome": "Novo Nome"}],
                "reparentar": [],  # vazio
                "mover": [{"acao": "mover_produto", "produto": 1, "para": [2]}],
                "arquivar": [],
                "verificar": [],
                "excluir": [{"acao": "excluir", "categoria": 3}],
            }
        }
        # Não deve levantar exceção
        validar_plano(plano, "bling")

    def test_validar_plano_estrutura_invalida(self):
        """Deve levantar ValueError se plano não tem estrutura esperada."""
        plano_invalido = {"fases": "nao eh dict"}
        with pytest.raises(ValueError, match="dicion"):
            validar_plano(plano_invalido, "bling")

        plano_sem_fases = {}
        with pytest.raises(ValueError, match="fases"):
            validar_plano(plano_sem_fases, "bling")

        plano_fases_incompletas = {"fases": {"criar": []}}  # faltam outras fases
        with pytest.raises(ValueError, match="ausentes"):
            validar_plano(plano_fases_incompletas, "bling")

    def test_validar_plano_provider_desconhecido(self):
        """Deve levantar ValueError para provider desconhecido."""
        plano = {"fases": {k: [] for k in ("criar", "renomear", "reparentar", "mover", "arquivar", "verificar", "excluir")}}
        with pytest.raises(ValueError, match="Provider desconhecido"):
            validar_plano(plano, "magento")


# =============================================================================
# Fixtures para Task 2: ProviderFake e dados de teste
# =============================================================================

class ProviderFake(BaseMarketplaceProvider):
    """Provider fake em memória para testes do motor universal."""

    nome = "fake"

    def __init__(self, provider_nome: str = "nuvemshop"):
        self._provider_nome = provider_nome
        self.categorias: dict[int, Categoria] = {}
        self.produtos: dict[int, Produto] = {}
        self.proximo_id_cat = 1000
        self.proximo_id_prod = 100
        self.chamadas = []

    @property
    def nome(self) -> str:
        return self._provider_nome

    def listar_categorias(self) -> list[Categoria]:
        self.chamadas.append(("listar_categorias",))
        return list(self.categorias.values())

    def criar_categoria(self, dados: NovaCategoria) -> Categoria:
        self.chamadas.append(("criar_categoria", dados))
        pai_id = dados.get("pai_id")
        # Valida se pai existe (se fornecido)
        if pai_id is not None and pai_id not in self.categorias:
            raise ErroMarketplace(400, f"Categoria pai {pai_id} não existe")
        cid = self.proximo_id_cat
        self.proximo_id_cat += 1
        cat: Categoria = {
            "id": cid,
            "nome": dados["nome"],
            "pai_id": pai_id,
            "descricao": dados.get("descricao", ""),
            "reutilizada": False,
        }
        self.categorias[cid] = cat
        return cat

    def atualizar_categoria(self, id: int, dados: AlteracaoCategoria) -> Categoria:
        self.chamadas.append(("atualizar_categoria", id, dados))
        if id not in self.categorias:
            raise NaoEncontrado(404, f"Categoria {id} não encontrada")
        cat = self.categorias[id]
        if "nome" in dados:
            cat["nome"] = dados["nome"]
        if "pai_id" in dados:
            # Valida se novo pai existe
            novo_pai = dados["pai_id"]
            if novo_pai is not None and novo_pai not in self.categorias:
                raise ErroMarketplace(400, f"Categoria pai {novo_pai} não existe")
            cat["pai_id"] = novo_pai
        if "descricao" in dados:
            cat["descricao"] = dados["descricao"]
        return cat

    def deletar_categoria(self, id: int) -> None:
        self.chamadas.append(("deletar_categoria", id))
        if id not in self.categorias:
            raise NaoEncontrado(404, f"Categoria {id} não encontrada")
        # Verifica se tem produtos
        for prod in self.produtos.values():
            if id in prod["categorias"]:
                raise CategoriaComProdutos(409, f"Categoria {id} tem produtos")
        del self.categorias[id]

    def listar_produtos(self) -> list[Produto]:
        self.chamadas.append(("listar_produtos",))
        return list(self.produtos.values())

    def atualizar_produto(self, id: int, dados: AlteracaoProduto) -> Produto:
        self.chamadas.append(("atualizar_produto", id, dados))
        if id not in self.produtos:
            raise NaoEncontrado(404, f"Produto {id} não encontrado")
        prod = self.produtos[id]
        if "categorias" in dados:
            prod["categorias"] = list(dados["categorias"])
        if "publicado" in dados:
            prod["publicado"] = dados["publicado"]
        if "nome" in dados:
            prod["nome"] = dados["nome"]
        return prod

    # Helpers para testes
    def add_categoria(self, nome: str, pai_id: int | None = None) -> int:
        cid = self.proximo_id_cat
        self.proximo_id_cat += 1
        self.categorias[cid] = {
            "id": cid, "nome": nome, "pai_id": pai_id, "descricao": "", "reutilizada": False
        }
        return cid

    def add_produto(self, nome: str, categorias: list[int], publicado: bool = True) -> int:
        pid = self.proximo_id_prod
        self.proximo_id_prod += 1
        self.produtos[pid] = {
            "id": pid, "nome": nome, "sku": f"SKU-{pid}",
            "categorias": categorias, "publicado": publicado
        }
        return pid


@pytest.fixture
def provider_nuvemshop():
    """Provider fake simulando Nuvemshop."""
    p = ProviderFake("nuvemshop")
    # Categorias iniciais
    p.add_categoria("Automação", None)  # id 1000
    p.add_categoria("CLPs", 1000)       # id 1001
    p.add_categoria("Comutação", None)  # id 1002
    # Produtos iniciais
    p.add_produto("Produto A", [1000, 1001])
    p.add_produto("Produto B", [1002])
    return p


@pytest.fixture
def provider_bling():
    """Provider fake simulando Bling."""
    p = ProviderFake("bling")
    p.add_categoria("Automação", None)  # id 1000
    p.add_categoria("CLPs", 1000)       # id 1001
    p.add_categoria("Comutação", None)  # id 1002
    p.add_produto("Produto A", [1000, 1001])
    p.add_produto("Produto B", [1002])
    return p


@pytest.fixture
def taxonomia_v8_para_motor():
    """Taxonomia v8 para testes do motor universal."""
    return {
        "versao": 8,
        "nos": [
            {"id_alvo": "cat1", "nome": "Automação", "pai": None, "nuvemshop_id": 1000, "bling_id": 1000},
            {"id_alvo": "cat2", "nome": "CLPs", "pai": "cat1", "nuvemshop_id": 1001, "bling_id": 1001},
            {"id_alvo": "cat3", "nome": "Sensores", "pai": "cat1", "nuvemshop_id": None, "bling_id": None},
            {"id_alvo": "cat4", "nome": "Comutação", "pai": None, "nuvemshop_id": 1002, "bling_id": 1002},
        ]
    }


@pytest.fixture
def bling_mapping_para_motor():
    return {"cat1": 1000, "cat2": 1001, "cat4": 1002}


@pytest.fixture
def diario_refs_para_motor():
    return {"novo:cat3": 1003}


@pytest.fixture
def plano_exemplo():
    """Plano de exemplo com todas as fases."""
    return {
        "versao_plano": 1,
        "fases": {
            "criar": [
                {"seq": 1, "acao": "criar", "ref": "novo:cat3", "nome": "Sensores", "pai": "cat1"}
            ],
            "renomear": [
                {"seq": 2, "acao": "renomear", "categoria": "cat4", "nome": "Comutação Industrial"}
            ],
            "reparentar": [
                {"seq": 3, "acao": "reparentar", "categoria": "cat2", "para": "cat4"}
            ],
            "mover": [
                {"seq": 4, "acao": "mover_produto", "produto": 100, "de": ["cat1"], "para": ["novo:cat3"]}
            ],
            "arquivar": [
                {"seq": 5, "acao": "arquivar_produto", "produto": 101, "payload": {"publicado": False, "categorias": []}}
            ],
            "verificar": [
                {"seq": 6, "acao": "verificar_vazia", "categoria": "cat4"}
            ],
            "excluir": []
        }
    }


@pytest.fixture
def snapshot_antes_exemplo():
    """Snapshot de produtos antes da aplicação."""
    return {
        100: {"categorias": [1000], "publicado": True},
        101: {"categorias": [1002], "publicado": True},
    }


# =============================================================================
# Testes Task 2: DiarioAgnostico
# =============================================================================

class TestDiarioAgnostico:
    """Testes para o DiarioAgnostico."""

    def test_diario_agnostico_cabecalho_provider(self, tmp_path):
        """Cabeçalho deve conter provider, store_id/client_id, refs."""
        diario = DiarioAgnostico(
            tmp_path / "diario.json",
            {"tipo": "aplicacao", "provider": "bling", "store_id": "12345"}
        )
        dados = json.loads((tmp_path / "diario.json").read_text(encoding="utf-8"))
        assert dados["provider"] == "bling"
        assert dados["store_id"] == "12345"
        assert "refs" in dados
        assert dados["refs"] == {}
        assert "acoes_arquivo" in dados

    def test_diario_agnostico_registra_acoes_jsonl(self, tmp_path):
        """Ações devem ser gravadas linha a linha no JSONL, atômico."""
        diario = DiarioAgnostico(tmp_path / "diario.json", {"tipo": "aplicacao", "provider": "nuvemshop"})
        diario.registrar({"seq": 1, "acao": "criar", "status": "ok"})
        diario.registrar({"seq": 2, "acao": "renomear", "status": "ok"})
        diario.gravar()

        # Verifica JSONL
        jsonl = tmp_path / "diario.acoes.jsonl"
        linhas = jsonl.read_text(encoding="utf-8").strip().split("\n")
        assert len(linhas) == 2
        assert json.loads(linhas[0])["acao"] == "criar"
        assert json.loads(linhas[1])["acao"] == "renomear"

        # Verifica cabeçalho atualizado
        dados = json.loads((tmp_path / "diario.json").read_text(encoding="utf-8"))
        assert dados["registradas"] == 2

    def test_diario_agnostico_grava_a_cada_25_ou_criar(self, tmp_path):
        """gravar() chamado a cada 25 ações ou em ação 'criar'."""
        diario = DiarioAgnostico(tmp_path / "diario.json", {"tipo": "aplicacao", "provider": "nuvemshop"})
        # 24 ações normais - não grava
        for i in range(24):
            diario.registrar({"seq": i, "acao": "mover_produto", "status": "ok"})
        assert not (tmp_path / "diario.json").exists() or json.loads((tmp_path / "diario.json").read_text())["registradas"] == 0

        # 25ª ação - grava
        diario.registrar({"seq": 25, "acao": "mover_produto", "status": "ok"})
        assert json.loads((tmp_path / "diario.json").read_text())["registradas"] == 25

        # Ação criar - grava imediatamente
        diario.registrar({"seq": 26, "acao": "criar", "status": "ok", "ref": "novo:x"})
        assert json.loads((tmp_path / "diario.json").read_text())["registradas"] == 26

    def test_diario_agnostico_carrega_compatibilidade_antigo(self, tmp_path):
        """Deve ler diário antigo Nuvemshop (sem 'provider') assumindo 'nuvemshop'."""
        # Cria diário no formato antigo (sem campo provider)
        diario_antigo = {
            "tipo": "aplicacao",
            "store_id": "123",
            "refs": {"novo:cat1": 999},
            "acoes_arquivo": "diario.acoes.jsonl",
        }
        (tmp_path / "diario_antigo.json").write_text(json.dumps(diario_antigo), encoding="utf-8")
        (tmp_path / "diario_antigo.acoes.jsonl").write_text(
            '{"seq": 1, "acao": "criar", "status": "ok"}\n',
            encoding="utf-8"
        )

        dados = carregar_diario_agnostico(tmp_path / "diario_antigo.json")
        assert dados["provider"] == "nuvemshop"  # assumido
        assert dados["store_id"] == "123"
        assert dados["refs"]["novo:cat1"] == 999
        assert len(dados["acoes"]) == 1


# =============================================================================
# Testes Task 2: AplicadorUniversal
# =============================================================================

class TestAplicadorUniversal:
    """Testes para o AplicadorUniversal."""

    def test_aplicador_universal_criar_renomear_mover(self, provider_nuvemshop, taxonomia_v8_para_motor, plano_exemplo, snapshot_antes_exemplo, tmp_path):
        """Fluxo completo: criar, renomear, mover_produto."""
        from wago.taxonomia.id_resolver import IDResolver
        from wago.taxonomia.plano_validator import validar_plano

        bling_mapping = {"cat1": 1000, "cat2": 1001, "cat4": 1002}
        diario_refs = {}

        resolver = IDResolver("nuvemshop", taxonomia_v8_para_motor, bling_mapping, diario_refs)
        validar_plano(plano_exemplo, "nuvemshop")

        diario = DiarioAgnostico(tmp_path / "diario.json", {
            "tipo": "aplicacao", "provider": "nuvemshop", "store_id": "123"
        })

        # Testa apenas criar, renomear, mover (sem reparentar, arquivar, verificar que podem falhar)
        acoes = plano_exemplo["fases"]["criar"] + plano_exemplo["fases"]["renomear"] + plano_exemplo["fases"]["mover"]
        aplicador = AplicadorUniversal(provider_nuvemshop, resolver, diario, snapshot_antes_exemplo, log=lambda m: None)
        stats = aplicador.executar(acoes)

        assert stats["ok"] == 3  # criar, renomear, mover
        assert stats["falhas"] == 0

        # Verifica se categoria foi criada
        cat_criada = [c for c in provider_nuvemshop.categorias.values() if c["nome"] == "Sensores"]
        assert len(cat_criada) == 1
        assert cat_criada[0]["pai_id"] == 1000

        # Verifica renomear
        cat_renomeada = provider_nuvemshop.categorias[1002]
        assert cat_renomeada["nome"] == "Comutação Industrial"

        # Verifica refs no diário
        dados_diario = json.loads((tmp_path / "diario.json").read_text(encoding="utf-8"))
        assert "novo:cat3" in dados_diario["refs"]

    def test_aplicador_universal_erro_isolado_continua(self, provider_nuvemshop, taxonomia_v8_para_motor, tmp_path):
        """Exceção em uma ação não deve parar o lote."""
        from wago.taxonomia.id_resolver import IDResolver
        from wago.taxonomia.plano_validator import validar_plano

        resolver = IDResolver("nuvemshop", taxonomia_v8_para_motor, {}, {})
        plano = {
            "fases": {k: [] for k in ("criar", "renomear", "reparentar", "mover", "arquivar", "verificar", "excluir")}
        }
        plano["fases"]["criar"] = [
            {"seq": 1, "acao": "criar", "ref": "novo:ok1", "nome": "OK1", "pai": None},
            {"seq": 2, "acao": "criar", "ref": "novo:falha", "nome": "Vai Falhar", "pai": 99999},  # pai inexistente
            {"seq": 3, "acao": "criar", "ref": "novo:ok2", "nome": "OK2", "pai": None},
        ]
        validar_plano(plano, "nuvemshop")

        diario = DiarioAgnostico(tmp_path / "diario.json", {"tipo": "aplicacao", "provider": "nuvemshop", "store_id": "123"})
        acoes = plano["fases"]["criar"]
        aplicador = AplicadorUniversal(provider_nuvemshop, resolver, diario, {}, log=lambda m: None)
        stats = aplicador.executar(acoes)

        assert stats["ok"] == 2
        assert stats["falhas"] == 1
        assert stats["puladas"] == 0

        # Verifica diário tem falha registrada
        dados = carregar_diario_agnostico(tmp_path / "diario.json")
        statuses = [a["status"] for a in dados["acoes"]]
        assert statuses.count("ok") == 2
        assert statuses.count("falha") == 1

    def test_aplicador_universal_resolve_ids(self, provider_nuvemshop, taxonomia_v8_para_motor, diario_refs_para_motor, bling_mapping_para_motor, tmp_path):
        """Deve usar IDResolver para traduzir novo:... e id_alvo."""
        from wago.taxonomia.id_resolver import IDResolver
        from wago.taxonomia.plano_validator import validar_plano

        # Adiciona categoria pai 1003 que vem do diario_refs
        provider_nuvemshop.add_categoria("Cat3 Pai", None)  # id 1003

        resolver = IDResolver("nuvemshop", taxonomia_v8_para_motor, bling_mapping_para_motor, diario_refs_para_motor)
        plano = {
            "fases": {k: [] for k in ("criar", "renomear", "reparentar", "mover", "arquivar", "verificar", "excluir")}
        }
        plano["fases"]["criar"] = [
            {"seq": 1, "acao": "criar", "ref": "novo:nova", "nome": "Nova Cat", "pai": "novo:cat3"}  # pai vem do diário
        ]
        plano["fases"]["mover"] = [
            {"seq": 2, "acao": "mover_produto", "produto": 100, "de": ["cat1"], "para": ["cat2"]}
        ]
        validar_plano(plano, "nuvemshop")

        # Inicializa diário com refs iniciais (para resolver referências novo:...)
        diario = DiarioAgnostico(tmp_path / "diario.json", {
            "tipo": "aplicacao", "provider": "nuvemshop", "store_id": "456",
            "refs": diario_refs_para_motor.copy()
        })
        acoes = plano["fases"]["criar"] + plano["fases"]["mover"]
        aplicador = AplicadorUniversal(provider_nuvemshop, resolver, diario, {100: {"categorias": [1000], "publicado": True}}, log=lambda m: None)
        stats = aplicador.executar(acoes)

        assert stats["ok"] == 2
        # Verifica se pai foi resolvido para 1003 (do diario_refs)
        cat_nova = [c for c in provider_nuvemshop.categorias.values() if c["nome"] == "Nova Cat"]
        assert len(cat_nova) == 1
        assert cat_nova[0]["pai_id"] == 1003

    def test_aplicador_universal_progresso_log(self, provider_nuvemshop, taxonomia_v8_para_motor, tmp_path):
        """Deve chamar log de progresso a cada N ações."""
        from wago.taxonomia.id_resolver import IDResolver
        from wago.taxonomia.plano_validator import validar_plano

        resolver = IDResolver("nuvemshop", taxonomia_v8_para_motor, {}, {})
        plano = {
            "fases": {k: [] for k in ("criar", "renomear", "reparentar", "mover", "arquivar", "verificar", "excluir")}
        }
        for i in range(5):
            plano["fases"]["criar"].append({"seq": i+1, "acao": "criar", "ref": f"novo:cat{i}", "nome": f"Cat{i}", "pai": None})
        validar_plano(plano, "nuvemshop")

        logs = []
        diario = DiarioAgnostico(tmp_path / "diario.json", {"tipo": "aplicacao", "provider": "nuvemshop", "store_id": "123"})
        acoes = plano["fases"]["criar"]
        aplicador = AplicadorUniversal(provider_nuvemshop, resolver, diario, {}, log=lambda m: logs.append(m), progresso_cada=2)
        aplicador.executar(acoes)

        # Deve ter logs de progresso (a cada 2 ações + final)
        progress_logs = [l for l in logs if "Progresso" in l or "progresso" in l.lower()]
        assert len(progress_logs) >= 2


# =============================================================================
# Testes Task 2: reverter_universal
# =============================================================================

class TestReverterUniversal:
    """Testes para o reverter_universal."""

    def test_reverter_universal_ordem_reversa(self, provider_nuvemshop, taxonomia_v8_para_motor, diario_refs_para_motor, bling_mapping_para_motor, tmp_path):
        """Rollback deve executar ações na ordem reversa correta."""
        from wago.taxonomia.id_resolver import IDResolver
        from wago.taxonomia.plano_validator import validar_plano
        from wago.taxonomia.motor_universal import AplicadorUniversal, DiarioAgnostico

        # Primeiro aplica algumas ações
        resolver = IDResolver("nuvemshop", taxonomia_v8_para_motor, bling_mapping_para_motor, diario_refs_para_motor)
        plano = {
            "fases": {k: [] for k in ("criar", "renomear", "reparentar", "mover", "arquivar", "verificar", "excluir")}
        }
        plano["fases"]["criar"] = [{"seq": 1, "acao": "criar", "ref": "novo:catX", "nome": "Cat X", "pai": None}]
        plano["fases"]["renomear"] = [{"seq": 2, "acao": "renomear", "categoria": "cat1", "nome": "Automação Nova"}]
        plano["fases"]["mover"] = [{"seq": 3, "acao": "mover_produto", "produto": 100, "de": ["cat1"], "para": ["cat2"]}]
        validar_plano(plano, "nuvemshop")

        diario = DiarioAgnostico(tmp_path / "diario.json", {"tipo": "aplicacao", "provider": "nuvemshop", "store_id": "123"})
        acoes = plano["fases"]["criar"] + plano["fases"]["renomear"] + plano["fases"]["mover"]
        aplicador = AplicadorUniversal(provider_nuvemshop, resolver, diario, {100: {"categorias": [1000], "publicado": True}}, log=lambda m: None)
        aplicador.executar(acoes)

        # Agora reverte
        diario_reversao = reverter_universal(tmp_path / "diario.json", provider_nuvemshop, log=lambda m: None)
        dados_rev = carregar_diario_agnostico(diario_reversao)

        # Verifica ordem: ORDEM_REVERSAO = excluir, reparentar, renomear, arquivar, mover, verificar, criar
        # Ações executadas: criar (seq=1), renomear (seq=2), mover (seq=3)
        # Ordem reversa: renomear (fase 2), mover (fase 4), criar (fase 6)
        acoes_rev = [a["acao"] for a in dados_rev["acoes"] if a["status"] == "ok"]
        assert acoes_rev == ["reverter_renomear", "reverter_mover_produto", "reverter_criar"]

        # Verifica estado restaurado
        assert provider_nuvemshop.categorias[1000]["nome"] == "Automação"  # renomear revertido
        assert 100 in provider_nuvemshop.produtos
        assert provider_nuvemshop.produtos[100]["categorias"] == [1000]  # mover revertido
        # Categoria criada deve ter sido excluída
        cats_criadas = [c for c in provider_nuvemshop.categorias.values() if c["nome"] == "Cat X"]
        assert len(cats_criadas) == 0

    def test_reverter_universal_reparentar_irreversivel_bling(self, provider_bling, taxonomia_v8_para_motor, bling_mapping_para_motor, tmp_path):
        """No Bling, rollback de reparentar deve marcar como irreversível."""
        from wago.taxonomia.id_resolver import IDResolver
        from wago.taxonomia.plano_validator import validar_plano
        from wago.taxonomia.motor_universal import AplicadorUniversal, DiarioAgnostico

        # Cria categoria extra para testar reparentar
        provider_bling.add_categoria("Nova Pai", None)  # id 1003

        resolver = IDResolver("bling", taxonomia_v8_para_motor, bling_mapping_para_motor, {})
        plano = {
            "fases": {k: [] for k in ("criar", "renomear", "reparentar", "mover", "arquivar", "verificar", "excluir")}
        }
        # No Bling, validador bloqueia reparentar, mas testamos o comportamento do rollback
        # se uma ação de reparentar tivesse sido executada (ex: via Nuvemshop e depois rollback no Bling)
        plano["fases"]["reparentar"] = [{"seq": 1, "acao": "reparentar", "categoria": "cat2", "para": "cat4"}]
        # Força validação passando (simula que foi executado em outro provider)
        plano["fases"]["reparentar"] = []

        # Simula diário com ação de reparentar já executada (status ok)
        diario = DiarioAgnostico(tmp_path / "diario.json", {"tipo": "aplicacao", "provider": "bling", "store_id": "456"})
        diario.registrar({
            "seq": 1, "acao": "reparentar", "status": "ok",
            "categoria": 1001, "antes": {"pai_id": 1000}, "depois": {"pai_id": 1002}
        })
        diario.gravar()

        # Tenta reverter
        diario_rev = reverter_universal(tmp_path / "diario.json", provider_bling, log=lambda m: None)
        dados_rev = carregar_diario_agnostico(diario_rev)

        acao_rev = next(a for a in dados_rev["acoes"] if a["acao"] == "reverter_reparentar")
        # Bling não suporta reparentar, então deve marcar como falha/irreversível
        assert acao_rev["status"] == "falha"
        assert "não suporta" in acao_rev.get("erro", "").lower() or "irrevers" in acao_rev.get("erro", "").lower()

def test_replace_robusto_tenta_de_novo_em_permission_error(tmp_path, monkeypatch):
    from wago.taxonomia.motor_universal import replace_robusto
    tmp = tmp_path / "a.tmp"
    destino = tmp_path / "a.json"
    tmp.write_text("dados", encoding="utf-8")
    chamadas = {"n": 0}
    original = type(tmp).replace

    def flaky(self, alvo):
        chamadas["n"] += 1
        if chamadas["n"] < 3:
            raise PermissionError(5, "Acesso negado")
        return original(self, alvo)

    monkeypatch.setattr(type(tmp), "replace", flaky)
    replace_robusto(tmp, destino, tentativas=5)
    assert destino.read_text(encoding="utf-8") == "dados"
    assert chamadas["n"] == 3


def test_replace_robusto_fallback_escrita_direta(tmp_path, monkeypatch):
    from wago.taxonomia.motor_universal import replace_robusto
    tmp = tmp_path / "b.tmp"
    destino = tmp_path / "b.json"
    tmp.write_text("dados", encoding="utf-8")

    def sempre(self, alvo):
        raise PermissionError(5, "Acesso negado")

    monkeypatch.setattr(type(tmp), "replace", sempre)
    replace_robusto(tmp, destino, tentativas=2)
    assert destino.read_text(encoding="utf-8") == "dados"
    assert not tmp.exists()
