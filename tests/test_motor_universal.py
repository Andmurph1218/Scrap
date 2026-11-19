"""Testes para o motor universal de taxonomia (IDResolver, plano_validator, etc.)."""

import pytest
from wago.taxonomia.id_resolver import IDResolver
from wago.taxonomia.plano_validator import validar_plano


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