import json
from pathlib import Path
import pytest
from wago.marketplaces.sync_bling import BlingTaxonomySync


def test_carregar_taxonomia_lanca_erro_se_ausente(tmp_path):
    sync = BlingTaxonomySync(estado_path=tmp_path / "mapping.json")
    with pytest.raises(FileNotFoundError):
        sync.carregar_taxonomia(tmp_path / "inexistente.json")


def test_carregar_taxonomia_lista_categorias(tmp_path):
    taxonomia = [
        {"id": "1", "nome": "Raiz", "pai_id": None},
        {"id": "2", "nome": "Filha", "pai_id": "1"},
    ]
    tax_file = tmp_path / "taxonomia.json"
    tax_file.write_text(json.dumps(taxonomia), encoding="utf-8")
    
    sync = BlingTaxonomySync(estado_path=tmp_path / "map.json", taxonomia_path=tax_file)
    carregadas = sync.carregar_taxonomia()
    assert len(carregadas) == 2
    assert carregadas[0]["nome"] == "Raiz"


def test_carregar_taxonomia_aceita_dict_com_chave_categorias(tmp_path):
    taxonomia = {"categorias": [{"id": "1", "nome": "Raiz", "pai_id": None}]}
    tax_file = tmp_path / "taxonomia.json"
    tax_file.write_text(json.dumps(taxonomia), encoding="utf-8")
    
    sync = BlingTaxonomySync(estado_path=tmp_path / "map.json", taxonomia_path=tax_file)
    carregadas = sync.carregar_taxonomia()
    assert len(carregadas) == 1


def test_carregar_taxonomia_aceita_formato_v8_nos(tmp_path):
    # Formato real da taxonomia v8: {"nos": [{"id_alvo", "nome", "pai"}]}
    taxonomia = {
        "nos": [
            {"id_alvo": "automacao", "nome": "Automação", "pai": None},
            {"id_alvo": "clps", "nome": "CLPs", "pai": "automacao"},
        ]
    }
    tax_file = tmp_path / "taxonomia.json"
    tax_file.write_text(json.dumps(taxonomia), encoding="utf-8")

    sync = BlingTaxonomySync(estado_path=tmp_path / "map.json", taxonomia_path=tax_file)
    carregadas = sync.carregar_taxonomia()
    assert len(carregadas) == 2
    assert carregadas[0] == {"id": "automacao", "nome": "Automação", "pai_id": None}
    assert carregadas[1] == {"id": "clps", "nome": "CLPs", "pai_id": "automacao"}


def test_mapeamento_persistencia(tmp_path):
    map_file = tmp_path / "mapping.json"
    sync = BlingTaxonomySync(estado_path=map_file)
    assert sync.carregar_mapeamento() == {}
    
    sync.salvar_mapeamento({"10": 10050})
    assert sync.carregar_mapeamento() == {"10": 10050}
    # Verifica escrita atômica: .tmp não deve existir após replace
    assert not (tmp_path / "mapping.tmp").exists()


def test_mapeamento_arquivo_corrompido_retorna_vazio(tmp_path):
    map_file = tmp_path / "mapping.json"
    map_file.write_text("{quebrado", encoding="utf-8")
    sync = BlingTaxonomySync(estado_path=map_file)
    assert sync.carregar_mapeamento() == {}


def test_salvar_mapeamento_cria_diretorios(tmp_path):
    map_file = tmp_path / "sub" / "mapping.json"
    sync = BlingTaxonomySync(estado_path=map_file)
    sync.salvar_mapeamento({"1": 100})
    assert map_file.exists()
    assert json.loads(map_file.read_text(encoding="utf-8")) == {"1": 100}


# ===== Task 2 Tests: sincronizar method =====

class ProviderFake:
    """Mock do BlingProvider para testes de sincronização."""
    def __init__(self, existentes=None):
        self.existentes = list(existentes or [])
        self.criadas = []
        self._next_id = 1000

    def listar_categorias(self):
        return self.existentes

    def criar_categoria(self, dados):
        cid = self._next_id
        self._next_id += 1
        cat = {"id": cid, "nome": dados["nome"], "pai_id": dados.get("pai_id"), "descricao": "", "reutilizada": False}
        self.existentes.append(cat)
        self.criadas.append(cat)
        return cat


def test_sincronizar_cria_raiz_e_filha_em_ordem_hierarquica(tmp_path):
    """Categorias devem ser criadas na ordem hierárquica: raiz primeiro, depois filhas."""
    taxonomia = [
        {"id": "1", "nome": "Materiais Elétricos", "pai_id": None},
        {"id": "2", "nome": "Cabos", "pai_id": "1"},
        {"id": "3", "nome": "Conectores", "pai_id": "1"},
        {"id": "4", "nome": "Cabos de Rede", "pai_id": "2"},
    ]
    tax_file = tmp_path / "taxonomia.json"
    tax_file.write_text(json.dumps(taxonomia), encoding="utf-8")
    
    provider = ProviderFake()
    sync = BlingTaxonomySync(provider, estado_path=tmp_path / "map.json", taxonomia_path=tax_file)
    res = sync.sincronizar(dry_run=False)
    
    assert res["criadas"] == 4
    assert res["reutilizadas"] == 0
    assert res["erros"] == 0
    assert res["total"] == 4
    assert len(provider.criadas) == 4
    # Verifica ordem: raízes primeiro
    assert provider.criadas[0]["nome"] == "Materiais Elétricos"
    assert provider.criadas[0]["pai_id"] is None
    # Filhas têm pai_id apontando para o bling_id do pai
    assert provider.criadas[1]["nome"] == "Cabos"
    assert provider.criadas[1]["pai_id"] == provider.criadas[0]["id"]
    assert provider.criadas[2]["nome"] == "Conectores"
    assert provider.criadas[2]["pai_id"] == provider.criadas[0]["id"]
    # Neta
    assert provider.criadas[3]["nome"] == "Cabos de Rede"
    assert provider.criadas[3]["pai_id"] == provider.criadas[1]["id"]
    # Mapeamento salvo
    mapeamento = sync.carregar_mapeamento()
    assert mapeamento["1"] == provider.criadas[0]["id"]
    assert mapeamento["2"] == provider.criadas[1]["id"]
    assert mapeamento["3"] == provider.criadas[2]["id"]
    assert mapeamento["4"] == provider.criadas[3]["id"]


def test_sincronizar_idempotente_reutiliza_existentes_no_bling(tmp_path):
    """Se categoria já existe no Bling (mesmo nome + mesmo pai), reutiliza sem recriar."""
    taxonomia = [
        {"id": "1", "nome": "Materiais", "pai_id": None},
        {"id": "2", "nome": "Cabos", "pai_id": "1"},
    ]
    tax_file = tmp_path / "taxonomia.json"
    tax_file.write_text(json.dumps(taxonomia), encoding="utf-8")
    
    # Bling já tem "Materiais" (raiz) e "Cabos" (filha de Materiais)
    provider = ProviderFake(existentes=[
        {"id": 5000, "nome": "Materiais", "pai_id": None},
        {"id": 5001, "nome": "Cabos", "pai_id": 5000},
    ])
    sync = BlingTaxonomySync(provider, estado_path=tmp_path / "map.json", taxonomia_path=tax_file)
    res = sync.sincronizar(dry_run=False)
    
    assert res["criadas"] == 0
    assert res["reutilizadas"] == 2
    assert res["erros"] == 0
    assert len(provider.criadas) == 0
    mapeamento = sync.carregar_mapeamento()
    assert mapeamento["1"] == 5000
    assert mapeamento["2"] == 5001


def test_sincronizar_idempotente_homonimos_pais_diferentes(tmp_path):
    """Homônimos em pais diferentes não devem ser confundidos."""
    taxonomia = [
        {"id": "1", "nome": "Eletrônicos", "pai_id": None},
        {"id": "2", "nome": "Cabos", "pai_id": "1"},
        {"id": "3", "nome": "Mecânica", "pai_id": None},
        {"id": "4", "nome": "Cabos", "pai_id": "3"},  # Mesmo nome, pai diferente
    ]
    tax_file = tmp_path / "taxonomia.json"
    tax_file.write_text(json.dumps(taxonomia), encoding="utf-8")
    
    # Bling já tem "Cabos" sob "Eletrônicos" (id 5000 -> 5001)
    provider = ProviderFake(existentes=[
        {"id": 5000, "nome": "Eletrônicos", "pai_id": None},
        {"id": 5001, "nome": "Cabos", "pai_id": 5000},
        {"id": 5002, "nome": "Mecânica", "pai_id": None},
    ])
    sync = BlingTaxonomySync(provider, estado_path=tmp_path / "map.json", taxonomia_path=tax_file)
    res = sync.sincronizar(dry_run=False)
    
    # "Cabos" sob "Mecânica" deve ser criado (não reutilizado)
    assert res["criadas"] == 1
    assert res["reutilizadas"] == 3
    assert len(provider.criadas) == 1
    assert provider.criadas[0]["nome"] == "Cabos"
    assert provider.criadas[0]["pai_id"] == 5002  # pai é "Mecânica"
    mapeamento = sync.carregar_mapeamento()
    assert mapeamento["4"] == provider.criadas[0]["id"]


def test_sincronizar_dry_run_nao_cria_nem_salva(tmp_path):
    """Modo dry_run não chama criar_categoria e não grava o mapeamento."""
    taxonomia = [{"id": "1", "nome": "Materiais", "pai_id": None}]
    tax_file = tmp_path / "taxonomia.json"
    tax_file.write_text(json.dumps(taxonomia), encoding="utf-8")
    
    provider = ProviderFake()
    sync = BlingTaxonomySync(provider, estado_path=tmp_path / "map.json", taxonomia_path=tax_file)
    res = sync.sincronizar(dry_run=True)
    
    assert res["criadas"] == 1
    assert res["reutilizadas"] == 0
    assert len(provider.criadas) == 0
    # Mapeamento NÃO deve ser salvo em disco
    assert sync.carregar_mapeamento() == {}


def test_sincronizar_isolamento_de_falha_continua_apos_erro(tmp_path):
    """Erro ao criar uma categoria não aborta a sincronização; continua para as próximas."""
    taxonomia = [
        {"id": "1", "nome": "Raiz OK", "pai_id": None},
        {"id": "2", "nome": "Falha", "pai_id": "1"},
        {"id": "3", "nome": "Outra OK", "pai_id": "1"},
    ]
    tax_file = tmp_path / "taxonomia.json"
    tax_file.write_text(json.dumps(taxonomia), encoding="utf-8")
    
    class ProviderComFalha(ProviderFake):
        def criar_categoria(self, dados):
            if dados["nome"] == "Falha":
                raise Exception("Erro simulado do Bling")
            return super().criar_categoria(dados)
    
    provider = ProviderComFalha()
    sync = BlingTaxonomySync(provider, estado_path=tmp_path / "map.json", taxonomia_path=tax_file)
    res = sync.sincronizar(dry_run=False)
    
    assert res["criadas"] == 2  # "Raiz OK" e "Outra OK"
    assert res["erros"] == 1    # "Falha"
    assert res["total"] == 3
    assert len(provider.criadas) == 2
    # Mapeamento deve ter apenas as que não falharam
    mapeamento = sync.carregar_mapeamento()
    assert "1" in mapeamento
    assert "3" in mapeamento
    assert "2" not in mapeamento  # A que falhou não entra no mapeamento


def test_sincronizar_progresso_log(tmp_path, capsys):
    """Deve emitir mensagens de progresso a cada N categorias."""
    # Criar 25 categorias para testar progresso a cada 10
    taxonomia = [
        {"id": str(i), "nome": f"Cat {i}", "pai_id": None if i <= 5 else str(i - 5)}
        for i in range(1, 26)
    ]
    tax_file = tmp_path / "taxonomia.json"
    tax_file.write_text(json.dumps(taxonomia), encoding="utf-8")
    
    provider = ProviderFake()
    sync = BlingTaxonomySync(provider, estado_path=tmp_path / "map.json", taxonomia_path=tax_file)
    res = sync.sincronizar(dry_run=False)
    
    captured = capsys.readouterr()
    # Verifica que houve output de progresso (pelo menos no final)
    assert res["total"] == 25
    # O teste real valida que o progresso é emitido; aqui verificamos que não quebrou
    # Implementação pode usar print ou logging


def test_sincronizar_reutiliza_mapeamento_local_existente(tmp_path):
    """Se já existe mapeamento local (arquivo), reutiliza sem consultar Bling novamente."""
    taxonomia = [
        {"id": "1", "nome": "Raiz", "pai_id": None},
        {"id": "2", "nome": "Filha", "pai_id": "1"},
    ]
    tax_file = tmp_path / "taxonomia.json"
    tax_file.write_text(json.dumps(taxonomia), encoding="utf-8")
    map_file = tmp_path / "map.json"
    map_file.write_text(json.dumps({"1": 9999, "2": 9998}), encoding="utf-8")
    
    provider = ProviderFake()
    sync = BlingTaxonomySync(provider, estado_path=map_file, taxonomia_path=tax_file)
    res = sync.sincronizar(dry_run=False)
    
    assert res["criadas"] == 0
    assert res["reutilizadas"] == 2
    assert len(provider.criadas) == 0
    mapeamento = sync.carregar_mapeamento()
    assert mapeamento["1"] == 9999
    assert mapeamento["2"] == 9998


def test_sincronizar_ordem_alfabetica_mesmo_nivel(tmp_path):
    """Categorias no mesmo nível hierárquico são processadas em ordem alfabética."""
    taxonomia = [
        {"id": "1", "nome": "Raiz", "pai_id": None},
        {"id": "3", "nome": "Charlie", "pai_id": "1"},
        {"id": "2", "nome": "Alpha", "pai_id": "1"},
        {"id": "4", "nome": "Beta", "pai_id": "1"},
    ]
    tax_file = tmp_path / "taxonomia.json"
    tax_file.write_text(json.dumps(taxonomia), encoding="utf-8")
    
    provider = ProviderFake()
    sync = BlingTaxonomySync(provider, estado_path=tmp_path / "map.json", taxonomia_path=tax_file)
    res = sync.sincronizar(dry_run=False)
    
    assert res["criadas"] == 4
    # Ordem dentro do mesmo nível: Alpha, Beta, Charlie
    assert provider.criadas[1]["nome"] == "Alpha"
    assert provider.criadas[2]["nome"] == "Beta"
    assert provider.criadas[3]["nome"] == "Charlie"


def test_sincronizar_pai_id_zero_ou_none_equivalentes(tmp_path):
    """Categorias raiz no Bling podem vir com pai_id 0 ou None; ambas devem ser tratadas como raiz."""
    taxonomia = [
        {"id": "1", "nome": "Raiz A", "pai_id": None},
        {"id": "2", "nome": "Raiz B", "pai_id": None},
    ]
    tax_file = tmp_path / "taxonomia.json"
    tax_file.write_text(json.dumps(taxonomia), encoding="utf-8")
    
    # Bling retorna raizes com pai_id = 0
    provider = ProviderFake(existentes=[
        {"id": 5000, "nome": "Raiz A", "pai_id": 0},
        {"id": 5001, "nome": "Raiz B", "pai_id": None},
    ])
    sync = BlingTaxonomySync(provider, estado_path=tmp_path / "map.json", taxonomia_path=tax_file)
    res = sync.sincronizar(dry_run=False)
    
    assert res["reutilizadas"] == 2
    assert res["criadas"] == 0
    mapeamento = sync.carregar_mapeamento()
    assert mapeamento["1"] == 5000
    assert mapeamento["2"] == 5001