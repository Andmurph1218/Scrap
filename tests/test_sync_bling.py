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
