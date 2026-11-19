"""Testes para migração de categorias antigas do Bling (Task 3)."""

import pytest
from typing import Any
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
from unidecode import unidecode


class ProviderFake(BaseMarketplaceProvider):
    """Provider fake em memória para testes de migração Bling."""

    nome = "bling"

    def __init__(self):
        self.categorias: dict[int, Categoria] = {}
        self.produtos: dict[int, Produto] = {}
        self.proximo_id_cat = 1000
        self.proximo_id_prod = 100
        self.chamadas = []

    @property
    def nome(self) -> str:
        return "bling"

    def listar_categorias(self) -> list[Categoria]:
        self.chamadas.append(("listar_categorias",))
        return list(self.categorias.values())

    def criar_categoria(self, dados: NovaCategoria) -> Categoria:
        self.chamadas.append(("criar_categoria", dados))
        pai_id = dados.get("pai_id")
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


# =============================================================================
# Fixtures: taxonomia v8 e bling_mapping simplificados para testes
# =============================================================================

@pytest.fixture
def taxonomia_v8_teste():
    """Taxonomia v8 de exemplo com bling_id."""
    return {
        "versao": 8,
        "nos": [
            {"id_alvo": "cat1", "nome": "Automação", "pai": None, "nuvemshop_id": 100, "bling_id": 10},
            {"id_alvo": "cat2", "nome": "CLPs", "pai": "cat1", "nuvemshop_id": 200, "bling_id": 20},
            {"id_alvo": "cat3", "nome": "Cabos e Fios", "pai": None, "nuvemshop_id": 300, "bling_id": 30},
            {"id_alvo": "cat4", "nome": "Comutação", "pai": None, "nuvemshop_id": 400, "bling_id": 40},
            {"id_alvo": "cat5", "nome": "Sensores", "pai": "cat1", "nuvemshop_id": None, "bling_id": 50},
        ]
    }


@pytest.fixture
def bling_mapping_teste():
    """Mapeamento id_alvo → bling_id (simula data/state/bling_mapping.json)."""
    return {
        "cat1": 10,
        "cat2": 20,
        "cat3": 30,
        "cat4": 40,
        "cat5": 50,
    }


@pytest.fixture
def provider_com_antigas_e_novas(taxonomia_v8_teste, bling_mapping_teste):
    """
    Provider com:
    - 3 categorias ANTIGAS (CAIXA ALTA, NÃO estão no bling_mapping):
        * "CABOS E FIOS" (id=1000) -> deve match "Cabos e Fios" (cat3, bling_id=30)
        * "TOMADAS" (id=1001) -> NÃO tem match nas novas v8
        * "COMUTACAO" (id=1002) -> deve match "Comutação" (cat4, bling_id=40) via fuzzy
    - 2 categorias NOVAS v8 (já mapeadas no bling_mapping):
        * "Cabos e Fios" (id=30)
        * "Comutação" (id=40)
    - Produtos nas categorias antigas
    """
    p = ProviderFake()

    # Categorias NOVAS v8 (já existem no Bling, ids = bling_id)
    p.add_categoria("Cabos e Fios", None)  # id 1000 -> mas bling_id=30, usamos id 30
    p.categorias[30] = {"id": 30, "nome": "Cabos e Fios", "pai_id": None, "descricao": "", "reutilizada": False}
    p.add_categoria("Comutação", None)  # id 1001 -> bling_id=40
    p.categorias[40] = {"id": 40, "nome": "Comutação", "pai_id": None, "descricao": "", "reutilizada": False}

    # Ajusta proximo_id_cat para não conflitar
    p.proximo_id_cat = 1000

    # Categorias ANTIGAS (CAIXA ALTA, NÃO estão no bling_mapping)
    # Nomes que fazem match exato após normalização (unidecode + lowercase + strip)
    id_antiga_cabos = p.add_categoria("CABOS E FIOS", None)  # 1000 -> normaliza para "cabos e fios"
    id_antiga_tomadas = p.add_categoria("TOMADAS", None)  # 1001 -> sem match
    id_antiga_interruptores = p.add_categoria("COMUTACAO", None)  # 1002 -> normaliza para "comutacao" = "Comutação"

    # Subcategoria antiga (para testar verificação de subcategorias antes de excluir)
    p.add_categoria("SUB CABOS", id_antiga_cabos)  # 1003

    # Produtos nas categorias antigas
    p.add_produto("Cabo PP 2.5mm", [id_antiga_cabos])  # 100
    p.add_produto("Cabo Flexível 4mm", [id_antiga_cabos])  # 101
    p.add_produto("Tomada 2P+T 10A", [id_antiga_tomadas])  # 102
    p.add_produto("Interruptor Simples", [id_antiga_interruptores])  # 103
    p.add_produto("Produto em ambas", [id_antiga_cabos, id_antiga_interruptores])  # 104

    return {
        "provider": p,
        "ids": {
            "antiga_cabos": id_antiga_cabos,
            "antiga_tomadas": id_antiga_tomadas,
            "antiga_interruptores": id_antiga_interruptores,
            "nova_cabos": 30,
            "nova_comutacao": 40,
        }
    }


# =============================================================================
# Testes: migrar_categorias_antigas_bling
# =============================================================================

class TestMigrarCategoriasAntigasBling:
    """Testes para a função migrar_categorias_antigas_bling."""

    def test_match_fuzzy_correto_cabos_eletricos_para_cabos_e_fios(self, provider_com_antigas_e_novas, taxonomia_v8_teste, bling_mapping_teste):
        """Deve fazer match fuzzy: 'CABOS ELETRICOS' -> 'Cabos e Fios' (lowercase + unidecode)."""
        from wago.taxonomia.migracao_bling import migrar_categorias_antigas_bling

        provider = provider_com_antigas_e_novas["provider"]
        ids = provider_com_antigas_e_novas["ids"]

        logs = []
        resultado = migrar_categorias_antigas_bling(
            provider=provider,
            taxonomia_v8=taxonomia_v8_teste,
            bling_mapping=bling_mapping_teste,
            log=logs.append,
            dry_run=False,
        )

        # Verifica que "CABOS ELETRICOS" fez match com "Cabos e Fios"
        assert resultado["migradas"] >= 1
        # Produtos da antiga "CABOS ELETRICOS" (id=1000) devem ter sido movidos para nova "Cabos e Fios" (id=30)
        prod_100 = provider.produtos[100]
        prod_101 = provider.produtos[101]
        prod_104 = provider.produtos[104]
        assert 30 in prod_100["categorias"]  # Cabo PP 2.5mm
        assert 30 in prod_101["categorias"]  # Cabo Flexível 4mm
        assert 30 in prod_104["categorias"]  # Produto em ambas
        assert ids["antiga_cabos"] not in prod_100["categorias"]
        assert ids["antiga_cabos"] not in prod_101["categorias"]
        assert ids["antiga_cabos"] not in prod_104["categorias"]

    def test_nao_match_ambiguo_ou_ausente_entra_em_nao_encontradas(self, provider_com_antigas_e_novas, taxonomia_v8_teste, bling_mapping_teste):
        """Categoria antiga sem correspondente ('TOMADAS') deve entrar em nao_encontradas com log explícito."""
        from wago.taxonomia.migracao_bling import migrar_categorias_antigas_bling

        provider = provider_com_antigas_e_novas["provider"]
        ids = provider_com_antigas_e_novas["ids"]

        logs = []
        resultado = migrar_categorias_antigas_bling(
            provider=provider,
            taxonomia_v8=taxonomia_v8_teste,
            bling_mapping=bling_mapping_teste,
            log=logs.append,
            dry_run=False,
        )

        # "TOMADAS" não tem match nas novas v8
        assert "TOMADAS" in resultado["nao_encontradas"]
        # Log explícito obrigatório
        log_msgs = "\n".join(logs)
        assert "[MIGRAÇÃO] SEM MATCH: 'TOMADAS'" in log_msgs
        assert f"id={ids['antiga_tomadas']}" in log_msgs
        assert "revisão manual necessária" in log_msgs

        # Produtos em TOMADAS não devem ser movidos
        prod_102 = provider.produtos[102]
        assert ids["antiga_tomadas"] in prod_102["categorias"]
        assert 30 not in prod_102["categorias"]  # não foi para Cabos e Fios
        assert 40 not in prod_102["categorias"]  # não foi para Comutação

    def test_migracao_produtos_move_da_antiga_para_nova(self, provider_com_antigas_e_novas, taxonomia_v8_teste, bling_mapping_teste):
        """Produtos devem ser movidos da categoria antiga para a nova (adiciona nova_id, remove antiga_id)."""
        from wago.taxonomia.migracao_bling import migrar_categorias_antigas_bling

        provider = provider_com_antigas_e_novas["provider"]
        ids = provider_com_antigas_e_novas["ids"]

        logs = []
        resultado = migrar_categorias_antigas_bling(
            provider=provider,
            taxonomia_v8=taxonomia_v8_teste,
            bling_mapping=bling_mapping_teste,
            log=logs.append,
            dry_run=False,
        )

        # "COMUTACAO" -> "Comutação" (match fuzzy)
        prod_103 = provider.produtos[103]
        prod_104 = provider.produtos[104]
        assert 40 in prod_103["categorias"]  # Interruptor Simples
        assert 40 in prod_104["categorias"]  # Produto em ambas
        assert ids["antiga_interruptores"] not in prod_103["categorias"]
        assert ids["antiga_interruptores"] not in prod_104["categorias"]

    def test_exclusao_apos_vazia_verifica_produtos_e_subcategorias(self, provider_com_antigas_e_novas, taxonomia_v8_teste, bling_mapping_teste):
        """Categoria antiga só é excluída se 0 produtos E 0 subcategorias."""
        from wago.taxonomia.migracao_bling import migrar_categorias_antigas_bling

        provider = provider_com_antigas_e_novas["provider"]
        ids = provider_com_antigas_e_novas["ids"]

        logs = []
        resultado = migrar_categorias_antigas_bling(
            provider=provider,
            taxonomia_v8=taxonomia_v8_teste,
            bling_mapping=bling_mapping_teste,
            log=logs.append,
            dry_run=False,
        )

        # "CABOS E FIOS" tinha subcategoria "SUB CABOS" -> NÃO deve ser excluída
        assert ids["antiga_cabos"] in provider.categorias
        # "COMUTACAO" não tinha subcategorias e produtos migraram -> deve ser excluída
        assert ids["antiga_interruptores"] not in provider.categorias
        assert resultado["excluidas"] >= 1

    def test_dry_run_nao_chama_escrita(self, provider_com_antigas_e_novas, taxonomia_v8_teste, bling_mapping_teste):
        """Em dry_run=True, não deve chamar atualizar_produto nem deletar_categoria."""
        from wago.taxonomia.migracao_bling import migrar_categorias_antigas_bling

        provider = provider_com_antigas_e_novas["provider"]
        ids = provider_com_antigas_e_novas["ids"]

        # Limpa chamadas anteriores
        provider.chamadas.clear()

        logs = []
        resultado = migrar_categorias_antigas_bling(
            provider=provider,
            taxonomia_v8=taxonomia_v8_teste,
            bling_mapping=bling_mapping_teste,
            log=logs.append,
            dry_run=True,
        )

        # Verifica que NÃO houve chamadas de escrita
        chamadas_escrita = [c for c in provider.chamadas if c[0] in ("atualizar_produto", "deletar_categoria")]
        assert len(chamadas_escrita) == 0, f"Houve chamadas de escrita em dry_run: {chamadas_escrita}"

        # Produtos não devem ter sido movidos
        prod_100 = provider.produtos[100]
        assert ids["antiga_cabos"] in prod_100["categorias"]
        assert 30 not in prod_100["categorias"]

        # Categorias antigas não devem ter sido excluídas
        assert ids["antiga_cabos"] in provider.categorias
        assert ids["antiga_interruptores"] in provider.categorias

    def test_isolamento_falhas_erro_em_uma_nao_para_outras(self, provider_com_antigas_e_novas, taxonomia_v8_teste, bling_mapping_teste):
        """Erro na migração de uma categoria não deve parar as demais."""
        from wago.taxonomia.migracao_bling import migrar_categorias_antigas_bling

        provider = provider_com_antigas_e_novas["provider"]
        ids = provider_com_antigas_e_novas["ids"]

        # Força erro ao atualizar produto 103 (Interruptor Simples)
        original_atualizar = provider.atualizar_produto
        def atualizar_com_erro(id, dados):
            if id == 103:
                raise ErroMarketplace(500, "Erro simulado")
            return original_atualizar(id, dados)
        provider.atualizar_produto = atualizar_com_erro

        logs = []
        resultado = migrar_categorias_antigas_bling(
            provider=provider,
            taxonomia_v8=taxonomia_v8_teste,
            bling_mapping=bling_mapping_teste,
            log=logs.append,
            dry_run=False,
        )

        # Mesmo com erro no produto 103, as outras migrações devem continuar
        # CABOS E FIOS (produtos 100, 101, 104) deve ter migrado
        prod_100 = provider.produtos[100]
        prod_101 = provider.produtos[101]
        prod_104 = provider.produtos[104]
        assert 30 in prod_100["categorias"]
        assert 30 in prod_101["categorias"]
        assert 30 in prod_104["categorias"]

        # Erro deve ser contado
        assert resultado["erros"] >= 1

    def test_cache_listar_produtos_chamado_uma_vez(self, provider_com_antigas_e_novas, taxonomia_v8_teste, bling_mapping_teste):
        """provider.listar_produtos() deve ser chamado UMA vez (cache), não N+1."""
        from wago.taxonomia.migracao_bling import migrar_categorias_antigas_bling

        provider = provider_com_antigas_e_novas["provider"]

        logs = []
        migrar_categorias_antigas_bling(
            provider=provider,
            taxonomia_v8=taxonomia_v8_teste,
            bling_mapping=bling_mapping_teste,
            log=logs.append,
            dry_run=False,
        )

        chamadas_listar = [c for c in provider.chamadas if c[0] == "listar_produtos"]
        assert len(chamadas_listar) == 1, f"listar_produtos chamado {len(chamadas_listar)} vezes, esperado 1"

    def test_normalizacao_unidecode_lowercase_strip(self, bling_mapping_teste):
        """Normalização deve usar unidecode + lowercase + strip em AMBOS os lados."""
        from wago.taxonomia.migracao_bling import migrar_categorias_antigas_bling

        # Taxonomia customizada para este teste
        taxonomia_custom = {
            "versao": 8,
            "nos": [
                {"id_alvo": "cat1", "nome": "Interruptores Eletricos", "pai": None, "nuvemshop_id": 100, "bling_id": 40},
            ]
        }
        mapping_custom = {"cat1": 40}

        p = ProviderFake()
        # Categoria antiga com acento e espaços
        id_antiga = p.add_categoria("  INTERRUPTORES ELÉTRICOS  ", None)
        # Nova categoria v8 já existe no provider (id=40)
        p.categorias[40] = {"id": 40, "nome": "Interruptores Eletricos", "pai_id": None, "descricao": "", "reutilizada": False}
        p.add_produto("Produto Teste", [id_antiga])

        logs = []
        resultado = migrar_categorias_antigas_bling(
            provider=p,
            taxonomia_v8=taxonomia_custom,
            bling_mapping=mapping_custom,
            log=logs.append,
            dry_run=False,
        )

        # Deve fazer match mesmo com acentos e espaços extras
        prod = p.produtos[100]
        assert 40 in prod["categorias"]
        assert id_antiga not in prod["categorias"]

    def test_retorno_estrutura_correta(self, provider_com_antigas_e_novas, taxonomia_v8_teste, bling_mapping_teste):
        """Retorno deve ter chaves: migradas, excluidas, nao_encontradas, erros."""
        from wago.taxonomia.migracao_bling import migrar_categorias_antigas_bling

        provider = provider_com_antigas_e_novas["provider"]

        logs = []
        resultado = migrar_categorias_antigas_bling(
            provider=provider,
            taxonomia_v8=taxonomia_v8_teste,
            bling_mapping=bling_mapping_teste,
            log=logs.append,
            dry_run=False,
        )

        assert set(resultado.keys()) == {"migradas", "excluidas", "nao_encontradas", "erros"}
        assert isinstance(resultado["migradas"], int)
        assert isinstance(resultado["excluidas"], int)
        assert isinstance(resultado["nao_encontradas"], list)
        assert isinstance(resultado["erros"], int)
        assert resultado["migradas"] >= 1
        assert "TOMADAS" in resultado["nao_encontradas"]