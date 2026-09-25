"""Escopo de produto (1.19.0): telemetria atribuida ao produto derivado.

Um satelite que hospeda produtos (o Forge, SDD §5.12 do ecossistema) grava o
consumo de cada execucao no produto que rodou. O que fica travado aqui e' o que,
errado, sai PLAUSIVEL: um run faturado no pai, um passo de ramo paralelo que
escapa do escopo, um lote de passos de dois produtos gravado como um so'.
"""
import contextvars
import threading

import pytest

from automaxia_utils.admin_center.service import AdminCenterConfig, AdminCenterService

PAI = "11111111-1111-1111-1111-111111111111"
AMBIENTE_DO_PAI = "22222222-2222-2222-2222-222222222222"
FILHO_A = "aaaaaaaa-0000-4000-8000-00000000000a"
FILHO_B = "bbbbbbbb-0000-4000-8000-00000000000b"
MODELO = "cccccccc-cccc-cccc-cccc-cccccccccccc"
AGENTE = "dddddddd-dddd-dddd-dddd-dddddddddddd"


@pytest.fixture
def svc(monkeypatch):
    s = AdminCenterService(AdminCenterConfig(enabled=False, product_id=PAI, environment_id=AMBIENTE_DO_PAI))
    s.config.enabled = True
    s.fila = []
    s.perguntas = []

    def _ep(agent_slug, product_id=None):
        s.perguntas.append((agent_slug, product_id))
        return {"agent_id": AGENTE, "model_id": MODELO, "model_name": "gpt-4o"}

    monkeypatch.setattr(s, "_enqueue_safely", lambda tipo, payload: s.fila.append((tipo, payload)) or True)
    monkeypatch.setattr(s, "get_effective_prompt", _ep)
    monkeypatch.setattr(s, "_get_model_id_by_name", lambda nome: MODELO)
    return s


def _ultimo(s, tipo):
    return [p for t, p in s.fila if t == tipo][-1]


# ------------------------------------------------------------ run e token

def test_fora_do_escopo_nada_muda(svc):
    svc.log_process("forge.execucao", "started")
    p = _ultimo(svc, "log_process")
    assert p["product_id"] == PAI and p["environment_id"] == AMBIENTE_DO_PAI


def test_run_dentro_do_escopo_vai_para_o_filho_sem_o_ambiente_do_pai(svc):
    with svc.product_scope(FILHO_A):
        svc.log_process("forge.execucao", "started")
    p = _ultimo(svc, "log_process")
    assert p["product_id"] == FILHO_A
    assert "environment_id" not in p


def test_product_id_explicito_vence_o_escopo(svc):
    with svc.product_scope(FILHO_A):
        svc.log_process("forge.execucao", "completed", product_id=FILHO_B)
    assert _ultimo(svc, "log_process")["product_id"] == FILHO_B


def test_token_no_escopo_passa_na_validacao_sem_ambiente(svc):
    with svc.product_scope(FILHO_A):
        assert svc.track_token_usage(model_name="gpt-4o", prompt_tokens=10, completion_tokens=5)
    p = _ultimo(svc, "token_usage")
    assert p["product_id"] == FILHO_A and "environment_id" not in p


def test_token_do_pai_ainda_exige_ambiente(svc):
    svc.environment_id = None
    svc.config.environment_id = None
    assert not svc.track_token_usage(model_name="gpt-4o", prompt_tokens=1, completion_tokens=1)


# ------------------------------------------------------------ passos e agente

def test_agent_step_resolve_o_agente_no_produto_do_escopo(svc):
    with svc.product_scope(FILHO_A):
        with svc.agent_step("analista", label="analisando"):
            pass
    passos = [p for t, p in svc.fila if t == "execution_step"]
    assert [p["status"] for p in passos] == ["running", "ok"]
    assert {p["product_id"] for p in passos} == {FILHO_A}
    assert all("environment_id" not in p for p in passos)
    # o modelo/agente veio do effective-prompt DO FILHO (override de modelo por produto)
    assert ("analista", FILHO_A) in svc.perguntas


def test_cache_do_agente_e_por_produto(svc):
    """Mesmo slug em dois produtos: modelos podem diferir (product_agents.model_id)."""
    with svc.product_scope(FILHO_A):
        svc.resolve_agent_id("analista")
    with svc.product_scope(FILHO_B):
        svc.resolve_agent_id("analista")
    assert [pid for _, pid in svc.perguntas] == [FILHO_A, FILHO_B]


def test_linha_final_fica_no_produto_da_entrada(svc):
    """A etapa fixa o produto ao entrar: sair do escopo antes de fechar a etapa
    nao pode mandar a linha final para o pai."""
    passo = svc.agent_step("analista", label="x")
    with svc.product_scope(FILHO_A):
        h = passo.__enter__()
    passo.__exit__(None, None, None)
    assert {p["product_id"] for t, p in svc.fila if t == "execution_step"} == {FILHO_A}
    assert h._product_id == FILHO_A


# ------------------------------------------------------------ escopo

def test_escopo_aninhado_volta_ao_de_fora(svc):
    with svc.product_scope(FILHO_A):
        with svc.product_scope(FILHO_B):
            svc.log_process("x", "started")
        svc.log_process("y", "started")
    svc.log_process("z", "started")
    assert [p["product_id"] for t, p in svc.fila] == [FILHO_B, FILHO_A, PAI]


def test_id_invalido_falha_alto(svc):
    with pytest.raises(ValueError):
        with svc.product_scope("nao-e-uuid"):
            pass


def test_ramo_paralelo_leva_o_escopo_com_copy_context(svc):
    """E' assim que o FlowRunner despacha ramos: sem copiar o contexto, o passo
    do ramo cairia no produto pai."""
    with svc.product_scope(FILHO_A):
        ctx = contextvars.copy_context()
    t = threading.Thread(target=ctx.run, args=(svc.log_process, "ramo", "started"))
    t.start(); t.join()
    assert _ultimo(svc, "log_process")["product_id"] == FILHO_A


# ------------------------------------------------------------ lote

def test_lote_de_passos_sai_um_post_por_produto(svc, monkeypatch):
    posts = []
    monkeypatch.setattr(svc, "_make_request", lambda metodo, endpoint, dados=None, **kw: posts.append(dados) or {"success": True})
    monkeypatch.setattr(svc, "_envelope_aceito", lambda resp: True)
    lote = [("execution_step", {"product_id": FILHO_A, "label": "a1"}),
            ("execution_step", {"product_id": FILHO_B, "label": "b1"}),
            ("execution_step", {"product_id": FILHO_A, "label": "a2"})]
    svc._process_batch(lote)
    assert sorted([p["label"] for p in grupo] for grupo in posts) == [["a1", "a2"], ["b1"]]
