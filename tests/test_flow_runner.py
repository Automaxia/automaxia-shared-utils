"""FlowRunner (1.19.0): a semantica do fluxo e as regras que nao dependem dele.

Semantica: laco com limite, ramo morto que nao trava a juncao, ramos paralelos.
Regras: IA so' do AdminCenter, teste que nao age, telemetria no produto certo
(inclusive nos ramos paralelos, que rodam em outra thread).
"""
import json

import pytest

from automaxia_utils.admin_center.service import AdminCenterConfig, AdminCenterService
from automaxia_utils.flows import (FlowRunner, PedidoLLM, RegistroDeFerramentas, RespostaLLM,
                                   validar_fluxo)

PAI = "11111111-1111-1111-1111-111111111111"
FILHO = "aaaaaaaa-0000-4000-8000-00000000000a"
MODELO = "cccccccc-cccc-cccc-cccc-cccccccccccc"


# ------------------------------------------------------------ fixtures

@pytest.fixture
def admin(monkeypatch):
    s = AdminCenterService(AdminCenterConfig(enabled=False, product_id=PAI,
                                             environment_id="22222222-2222-2222-2222-222222222222"))
    s.config.enabled = True
    s.fila = []
    s.sem_vinculo = set()
    s.sem_modelo = set()

    def _ep(agent_slug, product_id=None):
        if agent_slug in s.sem_vinculo:
            return None
        return {"agent_id": "dddddddd-dddd-dddd-dddd-dddddddddddd", "generic_content": f"AGENTE:{agent_slug}",
                "model_id": None if agent_slug in s.sem_modelo else MODELO,
                "model_name": None if agent_slug in s.sem_modelo else "gpt-4o"}

    monkeypatch.setattr(s, "_enqueue_safely", lambda tipo, payload: s.fila.append((tipo, payload)) or True)
    monkeypatch.setattr(s, "get_effective_prompt", _ep)
    monkeypatch.setattr(s, "_get_model_id_by_name", lambda nome: MODELO)
    return s


class LLM:
    """Responde pela fila do agente (lido do system prompt)."""

    def __init__(self, respostas):
        self.respostas = {k: list(v) for k, v in respostas.items()}
        self.pedidos = []

    def __call__(self, p: PedidoLLM) -> RespostaLLM:
        slug = p.sistema.split('AGENTE:')[1].split()[0]
        self.pedidos.append((slug, p))
        fila = self.respostas[slug]
        texto = fila.pop(0) if len(fila) > 1 else fila[0]
        return RespostaLLM(texto if isinstance(texto, str) else json.dumps(texto), 100, 20)


def ferramentas(validacoes=None):
    reg = RegistroDeFerramentas()
    chamadas = {"executar": 0, "email": 0}
    validacoes = list(validacoes or [True])

    @reg.ferramenta('validar_sql', entradas=['sql'], saida=['ok', 'erro'])
    def validar(ctx, ent, cfg):
        ok = validacoes.pop(0) if len(validacoes) > 1 else validacoes[0]
        return {'ok': ok, 'erro': None if ok else 'coluna valor_total nao existe'}

    @reg.ferramenta('executar_sql', entradas=['sql'], saida=['linhas'], selada=True)
    def executar(ctx, ent, cfg):
        chamadas["executar"] += 1
        return {'linhas': [{'regiao': 'Sul', 'total': 10}]}

    @reg.ferramenta('enviar_email', entradas=['corpo'], saida=['enviado'], efeito='email')
    def email(ctx, ent, cfg):
        chamadas["email"] += 1
        return {'enviado': True}

    return reg, chamadas


def fluxo_sql(com_email=False):
    nos = [
        {"id": "gerar", "tipo": "agente", "agente": "agent-sql", "rotulo": "Gerar SQL",
         "entradas": {"pergunta": "$entrada.pergunta"}, "saida": {"formato": "json", "campos": ["sql"]}},
        {"id": "validar", "tipo": "ferramenta", "ferramenta": "validar_sql", "rotulo": "Validar",
         "entradas": {"sql": "$nos.corretor.sql | $nos.gerar.sql"}},
        {"id": "ok", "tipo": "condicao", "rotulo": "SQL ok?",
         "regras": [{"ramo": "ok", "caminho": "$nos.validar.ok", "op": "igual", "valor": "true"}]},
        {"id": "corretor", "tipo": "agente", "agente": "corretor", "rotulo": "Corrigir",
         "entradas": {"sql": "$nos.corretor.sql | $nos.gerar.sql", "erro": "$nos.validar.erro"},
         "saida": {"formato": "json", "campos": ["sql"]}},
        {"id": "executar", "tipo": "ferramenta", "ferramenta": "executar_sql", "rotulo": "Executar",
         "entradas": {"sql": "$nos.corretor.sql | $nos.gerar.sql"}},
        {"id": "insight", "tipo": "agente", "agente": "insight", "rotulo": "Insight",
         "entradas": {"linhas": "$nos.executar.linhas"}, "saida": {"formato": "texto"}},
        {"id": "analise", "tipo": "agente", "agente": "analise", "rotulo": "Analise",
         "entradas": {"linhas": "$nos.executar.linhas"}, "saida": {"formato": "json", "campos": ["tendencia"]}},
        {"id": "fim", "tipo": "saida", "rotulo": "Resposta",
         "campos": {"resposta": "$nos.insight.texto", "tendencia": "$nos.analise.tendencia",
                    "sql": "$nos.corretor.sql | $nos.gerar.sql"}},
    ]
    arestas = [
        {"de": "entrada", "para": "gerar"}, {"de": "gerar", "para": "validar"}, {"de": "validar", "para": "ok"},
        {"de": "ok", "para": "executar", "ramo": "ok"}, {"de": "ok", "para": "corretor", "ramo": "senao"},
        {"de": "corretor", "para": "validar", "max_voltas": 2},
        {"de": "executar", "para": "insight"}, {"de": "executar", "para": "analise"},
        {"de": "insight", "para": "fim"}, {"de": "analise", "para": "fim"},
    ]
    if com_email:
        nos.append({"id": "email", "tipo": "ferramenta", "ferramenta": "enviar_email", "rotulo": "Mandar",
                    "entradas": {"corpo": "$nos.insight.texto"}, "config": {"destino": "d1"}})
        arestas = [a for a in arestas if a != {"de": "insight", "para": "fim"}]
        arestas += [{"de": "insight", "para": "email"}, {"de": "email", "para": "fim"}]
    return {"formato": 1, "nos": nos, "arestas": arestas}


def llm_padrao():
    return LLM({"agent-sql": [{"sql": "SELECT valor_total FROM v"}], "corretor": [{"sql": "SELECT valor_liquido FROM v"}],
                "insight": ["Sul lidera."], "analise": [{"tendencia": "alta"}]})


# ------------------------------------------------------------ semantica

def test_volta_de_correcao_e_juncao_dos_ramos_paralelos(admin):
    reg, chamadas = ferramentas(validacoes=[False, True])
    r = FlowRunner(admin, reg, llm_padrao()).run(fluxo_sql(), {"pergunta": "vendas por regiao"})
    assert r.ok, r.falha
    assert r.saida == {"resposta": "Sul lidera.", "tendencia": "alta", "sql": "SELECT valor_liquido FROM v"}
    ordem = [t["no"] for t in r.traco]
    assert ordem.count("validar") == 2 and ordem.count("corretor") == 1
    # a juncao esperou os dois ramos
    assert set(ordem[-3:]) == {"insight", "analise", "fim"} and ordem[-1] == "fim"
    assert chamadas["executar"] == 1


def test_volta_passa_do_limite(admin):
    reg, _ = ferramentas(validacoes=[False])
    r = FlowRunner(admin, reg, llm_padrao()).run(fluxo_sql(), {"pergunta": "x"})
    assert not r.ok and "volta" in r.falha


def test_ramo_morto_nao_roda(admin):
    fluxo = {"formato": 1, "nos": [
        {"id": "guard", "tipo": "agente", "agente": "guard", "entradas": {"p": "$entrada.pergunta"},
         "saida": {"formato": "escolha", "opcoes": ["dentro", "fora"]}},
        {"id": "caro", "tipo": "agente", "agente": "caro", "entradas": {"p": "$entrada.pergunta"}, "saida": {"formato": "texto"}},
        {"id": "sim", "tipo": "saida", "campos": {"resposta": "$nos.caro.texto"}},
        {"id": "nao", "tipo": "saida", "campos": {"resposta": "$nos.guard.motivo"}}],
        "arestas": [{"de": "entrada", "para": "guard"}, {"de": "guard", "para": "caro", "ramo": "dentro"},
                    {"de": "guard", "para": "nao", "ramo": "fora"}, {"de": "caro", "para": "sim"}]}
    llm = LLM({"guard": [{"escolha": "fora", "motivo": "fora do escopo"}], "caro": ["nunca"]})
    r = FlowRunner(admin, RegistroDeFerramentas(), llm).run(fluxo, {"pergunta": "piada"})
    assert r.saida == {"resposta": "fora do escopo"}
    assert [s for s, _ in llm.pedidos] == ["guard"]


# ------------------------------------------------------------ regras

def test_teste_nao_chama_a_ferramenta_de_efeito(admin):
    reg, chamadas = ferramentas()
    r = FlowRunner(admin, reg, llm_padrao()).run(fluxo_sql(com_email=True), {"pergunta": "x"}, modo="teste")
    assert r.ok and chamadas["email"] == 0
    passo = next(t for t in r.traco if t["no"] == "email")
    assert passo["simulado"] and passo["saida"]["faria"]["config"] == {"destino": "d1"}


def test_real_chama(admin):
    reg, chamadas = ferramentas()
    FlowRunner(admin, reg, llm_padrao()).run(fluxo_sql(com_email=True), {"pergunta": "x"}, modo="real")
    assert chamadas["email"] == 1


@pytest.mark.parametrize("falta,texto", [("sem_vinculo", "vinculado"), ("sem_modelo", "modelo")])
def test_ia_so_do_admincenter(admin, falta, texto):
    getattr(admin, falta).add("agent-sql")
    reg, _ = ferramentas()
    llm = llm_padrao()
    r = FlowRunner(admin, reg, llm).run(fluxo_sql(), {"pergunta": "x"})
    assert not r.ok and texto in r.falha and r.no_falha == "gerar"
    assert llm.pedidos == []  # nenhum LLM chamado com modelo/prompt inventado


def test_json_invalido_tem_uma_segunda_chance(admin):
    reg, _ = ferramentas()
    llm = LLM({"agent-sql": ["isso nao e json", {"sql": "SELECT 1"}], "insight": ["ok"], "analise": [{"tendencia": "x"}]})
    r = FlowRunner(admin, reg, llm).run(fluxo_sql(), {"pergunta": "x"})
    assert r.ok
    assert "Correcao" in [p for s, p in llm.pedidos if s == "agent-sql"][-1].usuario


def test_json_invalido_duas_vezes_falha_no_no(admin):
    reg, _ = ferramentas()
    llm = LLM({"agent-sql": ["nao", "nao"], "insight": ["ok"], "analise": [{"tendencia": "x"}]})
    r = FlowRunner(admin, reg, llm).run(fluxo_sql(), {"pergunta": "x"})
    assert not r.ok and r.no_falha == "gerar"


def test_limite_de_tokens(admin):
    reg, _ = ferramentas()
    r = FlowRunner(admin, reg, llm_padrao(), max_tokens=150).run(fluxo_sql(), {"pergunta": "x"})
    assert not r.ok and "tokens" in r.falha


# ------------------------------------------------------------ telemetria

def test_passos_e_tokens_no_produto_do_escopo_inclusive_nos_ramos_paralelos(admin):
    reg, _ = ferramentas()
    with admin.product_scope(FILHO):
        r = FlowRunner(admin, reg, llm_padrao()).run(fluxo_sql(), {"pergunta": "x"}, rotulo_versao="v3")
    passos = [p for t, p in admin.fila if t == "execution_step"]
    tokens = [p for t, p in admin.fila if t == "token_usage"]
    assert passos and {p["product_id"] for p in passos} == {FILHO}
    assert {p["product_id"] for p in tokens} == {FILHO}
    # a correlacao atravessou as threads dos ramos paralelos (insight e analise)
    assert {p.get("correlation_id") for p in passos} == {r.correlation_id}
    finais = [p for p in passos if p["status"] == "ok"]
    assert {p["label"] for p in finais} >= {"Insight", "Analise"}
    assert all(p["detail"]["flow_version"] == "v3" for p in finais)
    # seq unica mesmo com ramos em paralelo
    seqs = [p["seq"] for p in finais]
    assert len(seqs) == len(set(seqs))


# ------------------------------------------------------------ validacao

def test_fluxo_bom_nao_tem_problema():
    reg, _ = ferramentas()
    assert validar_fluxo(fluxo_sql(com_email=True), reg, entradas=["pergunta"], saidas=["resposta"]) == []


def test_validacao_pega_os_erros_plausiveis():
    reg, _ = ferramentas()
    f = fluxo_sql(com_email=True)
    f["arestas"] = [a for a in f["arestas"] if not a.get("max_voltas")] + [{"de": "corretor", "para": "validar"}]
    f["arestas"] = [a for a in f["arestas"] if a.get("ramo") != "ok"]
    next(n for n in f["nos"] if n["id"] == "email")["config"] = {}
    next(n for n in f["nos"] if n["id"] == "gerar")["entradas"]["x"] = "$nos.insight.texto"
    msgs = " | ".join(p["msg"] for p in validar_fluxo(f, reg, entradas=["pergunta"], saidas=["resposta"]))
    assert "laco sem limite" in msgs
    assert 'ramo "ok" nao leva' in msgs
    assert "destino" in msgs
    assert "nao roda antes" in msgs
