"""Camada de IA: provedores, limite do plano (pausa + aviso + retomada) e integração com o classificador."""

import json
from datetime import datetime, timezone

import pytest

from wago.taxonomia import classificador as cl
from wago.taxonomia import ia, modelo


class Resp:
    def __init__(self, status, corpo, headers=None):
        self.status_code = status
        self._c = corpo
        self.headers = headers or {}
        self.content = b"x"

    def json(self):
        return self._c


class Sessao:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.enviados = []

    def post(self, url, timeout=None, headers=None, json=None):
        self.enviados.append({"url": url, "headers": headers, "json": json})
        return self.respostas.pop(0)


def _ok_gemini(dados):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(dados)}]}}],
                      "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20}})


COTA_MINUTO = Resp(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"},
]}})
COTA_DIA = Resp(429, {"error": {"code": 429, "message": "Quota exceeded", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
]}})


def _gemini(respostas):
    return ia.GeminiProvider("chave-teste", "gemini-flash-lite-latest", session=Sessao(respostas), dormir=lambda s: None)


def test_gemini_envia_chave_no_header_json_e_conta_tokens():
    p = _gemini([_ok_gemini({"resultados": []})])
    assert p.gerar_json("oi") == {"resultados": []}
    env = p.session.enviados[0]
    assert env["headers"]["x-goog-api-key"] == "chave-teste" and "key=" not in env["url"]
    assert env["json"]["generationConfig"] == {"temperature": 0, "responseMimeType": "application/json"}
    assert p.uso.chamadas == 1 and p.uso.tokens_entrada == 100


def test_gemini_429_vira_cota_esgotada_com_tipo_e_espera():
    with pytest.raises(ia.CotaEsgotada) as exc:
        _gemini([COTA_MINUTO]).gerar_json("x")
    assert exc.value.espera_s == 37 and not exc.value.diaria
    with pytest.raises(ia.CotaEsgotada) as exc:
        _gemini([COTA_DIA]).gerar_json("x")
    assert exc.value.diaria


def test_resposta_invalida_e_contabilizada():
    p = _gemini([Resp(200, {"candidates": [{"content": {"parts": [{"text": "não é json"}]}}]})])
    with pytest.raises(ia.ErroIA):
        p.gerar_json("x")
    assert p.uso.respostas_invalidas == 1


def test_obter_provedor_auto_pela_chave(monkeypatch):
    for k in ("IA_PROVIDER", "GEMINI_API_KEY", "OPENAI_API_KEY", "IA_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert ia.obter_provedor() is None
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    assert ia.obter_provedor().nome == "openai"
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    p = ia.obter_provedor()
    assert p.nome == "gemini" and p.modelo == ia.GEMINI_MODELO_PADRAO


def _controle(provedor, **kw):
    esperas, logs, avisos = [], [], []
    agora = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    c = ia.ControleCota(provedor, log=logs.append, aviso=avisos.append, dormir=esperas.append,
                        agora=lambda: agora, rpm=6000, **kw)
    return c, esperas, logs, avisos


def test_limite_por_minuto_avisa_pausa_e_retoma_automaticamente():
    c, esperas, logs, avisos = _controle(_gemini([COTA_MINUTO, _ok_gemini({"resultados": [1]})]))
    assert c.chamar("x") == {"resultados": [1]}
    assert avisos[0]["tipo"] == "por_minuto" and avisos[0]["espera_s"] == 37
    assert sum(esperas) == pytest.approx(37, abs=0.5)
    assert "continuará automaticamente somente quando o plano liberar novo uso" in logs[0]
    assert logs[-1].startswith("Retomando")
    assert c.uso_pausa.pausas_cota == 1


def test_cota_diaria_espera_ate_renovacao_do_plano():
    c, esperas, logs, avisos = _controle(_gemini([COTA_DIA, _ok_gemini({"resultados": []})]))
    c.chamar("x")
    assert avisos[0]["tipo"] == "diaria"
    # 12:00 UTC = 04:00 no Pacífico → renovação ~00:05 PT do dia seguinte (≈ 20h)
    assert 19 * 3600 < sum(esperas) < 21 * 3600
    assert "cota diária" in logs[0]


def test_cancelar_durante_a_pausa_da_cota():
    estado = {"n": 0}

    def cancelado():
        estado["n"] += 1
        return estado["n"] > 2

    c, _, _, _ = _controle(_gemini([COTA_MINUTO]), cancelado=cancelado)
    with pytest.raises(ia.CanceladoIA):
        c.chamar("x")


# ---------------------------------------------------------------- integração com o classificador
class FakeProvedor:
    nome, modelo = "fake", "m1"

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.prompts = []
        self.uso = ia.Uso()

    def gerar_json(self, prompt):
        self.prompts.append(prompt)
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        self.uso.chamadas += 1
        return r


def _tax():
    nos = [modelo._no("materiais", "Materiais Elétricos", None), modelo._no("reles", "Relés", "materiais"),
           modelo._no("dr", "Interruptores Diferenciais", "materiais"), modelo._no("lampadas", "Lâmpadas", None)]
    return {"versao": 1, "nos": nos, "mapa_categorias_loja": {}}


SNAP = {"produtos": [
    {"id": 1, "nome": "Rele 24V", "categorias": [], "skus": [], "descricao_html": ""},
    {"id": 2, "nome": "DR RCCB 40A 30mA", "categorias": [], "skus": [], "descricao_html": "<p>Proteção</p>"},
    {"id": 3, "nome": "Calefator 250W", "categorias": [], "skus": [], "descricao_html": ""},
    {"id": 4, "nome": "Item genérico", "categorias": [], "skus": [], "descricao_html": ""},
]}


@pytest.fixture
def pasta(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "TAXONOMIA", tmp_path)
    return tmp_path


def test_ia_classifica_so_pendentes_aplica_regras_e_usa_cache(pasta):
    tax = _tax()
    res = cl.classificar_lote(SNAP, tax)
    assert [r["status"] for r in res].count(cl.STATUS_AUTO) == 1  # só o relé pelas regras
    resposta = {"resultados": [
        {"produto_id": 2, "categoria_id": "dr", "confianca": 0.93, "justificativa": "DR/RCCB"},
        {"produto_id": 3, "categoria_id": None, "confianca": 0.4, "justificativa": "sem categoria",
         "nova_categoria": {"nome": "Climatização de Painéis", "pai": "materiais", "justificativa": "calefator"}},
        {"produto_id": 4, "categoria_id": "acessorios", "confianca": 0.99, "justificativa": "x"},
    ]}
    prov = FakeProvedor([resposta])
    controle = ia.ControleCota(prov, log=lambda m: None, dormir=lambda s: None, rpm=6000)
    info = cl.classificar_com_ia(res, SNAP, tax, controle, log=lambda m: None)
    por_id = {r["produto_id"]: r for r in res}
    assert por_id[2]["status"] == cl.STATUS_AUTO and por_id[2]["origem"] == "ia" and por_id[2]["categoria_id"] == "dr"
    assert por_id[3]["status"] == cl.STATUS_REVISAO and por_id[3]["nova_categoria"]["nome"] == "Climatização de Painéis"
    assert por_id[4]["status"] == cl.STATUS_REVISAO  # categoria inexistente/genérica
    assert info["propostas"][0]["produtos"] == [3] and info["enviados"] == 3
    assert "Rele 24V" not in prov.prompts[0] and "<p>" not in prov.prompts[0]  # só pendentes, texto sanitizado
    assert "- dr: Materiais Elétricos > Interruptores Diferenciais" in prov.prompts[0]

    # segunda execução: tudo vem do cache, nenhuma chamada
    res2 = cl.classificar_lote(SNAP, tax)
    prov2 = FakeProvedor([])
    info2 = cl.classificar_com_ia(res2, SNAP, tax, ia.ControleCota(prov2, log=lambda m: None), log=lambda m: None)
    assert info2["do_cache"] == 3 and prov2.prompts == []
    assert {r["produto_id"]: r for r in res2}[2]["status"] == cl.STATUS_AUTO


def test_cota_no_meio_do_lote_pausa_e_conclui(pasta):
    tax = _tax()
    res = cl.classificar_lote(SNAP, tax)
    ok = {"resultados": [{"produto_id": 2, "categoria_id": "dr", "confianca": 0.9}]}
    prov = FakeProvedor([ia.CotaEsgotada("fake", "limite", espera_s=30), ok])
    avisos = []
    controle = ia.ControleCota(prov, log=lambda m: None, aviso=avisos.append, dormir=lambda s: None, rpm=6000)
    info = cl.classificar_com_ia(res, SNAP, tax, controle, log=lambda m: None, lote=25)
    assert avisos and info["pausas_cota"] == 1
    assert {r["produto_id"]: r for r in res}[2]["status"] == cl.STATUS_AUTO


def test_cancelamento_preserva_cache_para_retomar(pasta):
    tax = _tax()
    res = cl.classificar_lote(SNAP, tax)
    lote1 = {"resultados": [{"produto_id": 2, "categoria_id": "dr", "confianca": 0.9}]}
    prov = FakeProvedor([lote1, ia.CotaEsgotada("fake", "limite", espera_s=600)])
    chamadas = {"n": 0}

    def cancelado():
        chamadas["n"] += 1
        return chamadas["n"] > 4

    controle = ia.ControleCota(prov, log=lambda m: None, dormir=lambda s: None, rpm=6000, cancelado=cancelado)
    info = cl.classificar_com_ia(res, SNAP, tax, controle, log=lambda m: None, lote=1)
    assert info["interrompido"]
    assert len(json.loads((pasta / "cache_ia.json").read_text(encoding="utf-8"))) == 1
