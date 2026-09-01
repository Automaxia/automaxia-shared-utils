"""Dimensoes de custo por agente no token usage (1.14.0).

Fecha a lacuna que a 1.14.0 herdou: o `counter.py` nunca preenchia
`agent_id`/`area_agent_id`, entao o ranking de custo por agente ficava vazio.
"""
import pytest

from automaxia_utils.admin_center.service import AdminCenterConfig, AdminCenterService

AG_ETAPA = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
AG_AREA = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
MODELO = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _service_agentes(monkeypatch, efetivos):
    """Service com o effective-prompt fingido. `efetivos` = {slug: payload}.

    Devolve (service, lista de slugs perguntados) para contar roundtrips.
    """
    svc = AdminCenterService(AdminCenterConfig(
        enabled=False,
        product_id="11111111-1111-1111-1111-111111111111",
        environment_id="22222222-2222-2222-2222-222222222222",
    ))
    svc.config.enabled = True
    perguntas = []

    def _ep(agent_slug, product_id=None):
        perguntas.append(agent_slug)
        return efetivos.get(agent_slug)

    monkeypatch.setattr(svc, "get_effective_prompt", _ep)
    monkeypatch.setattr(svc, "_get_model_id_by_name", lambda nome: MODELO)
    return svc, perguntas


class TestResolveAgentId:
    def test_resolve_pelo_effective_prompt(self, monkeypatch):
        svc, _ = _service_agentes(monkeypatch, {"sql": {"agent_id": AG_ETAPA}})
        assert svc.resolve_agent_id("sql") == AG_ETAPA

    def test_segunda_chamada_vem_do_cache(self, monkeypatch):
        svc, perguntas = _service_agentes(
            monkeypatch, {"sql": {"agent_id": AG_ETAPA, "model_id": MODELO}}
        )
        assert svc.resolve_agent_id("sql") == AG_ETAPA
        assert svc.resolve_agent_id("sql") == AG_ETAPA
        assert perguntas == ["sql"]

    def test_agente_sem_modelo_ainda_cacheia_o_id(self, monkeypatch):
        """Sem isso, agente sem modelo pagava roundtrip a cada chamada de LLM
        so para descobrir o agent_id."""
        svc, perguntas = _service_agentes(
            monkeypatch, {"sql": {"agent_id": AG_ETAPA, "model_id": None}}
        )
        assert svc.resolve_agent_id("sql") == AG_ETAPA
        assert svc.resolve_agent_id("sql") == AG_ETAPA
        assert perguntas == ["sql"]

    def test_modelo_pendente_continua_reperguntando(self, monkeypatch):
        """O id e estavel; o modelo pode ser configurado no painel a qualquer
        momento, entao esse lado nao pode congelar em None."""
        svc, perguntas = _service_agentes(
            monkeypatch, {"sql": {"agent_id": AG_ETAPA, "model_id": None}}
        )
        assert svc._resolve_agent_model("sql") == (None, None)
        assert svc._resolve_agent_model("sql") == (None, None)
        assert len(perguntas) == 2

    def test_negativa_e_cacheada(self, monkeypatch):
        """Slug que nao resolve nao pode somar um roundtrip HTTP a cada chamada
        de LLM — telemetria nao pode custar mais que o trabalho que ela mede."""
        svc, perguntas = _service_agentes(monkeypatch, {})
        assert svc.resolve_agent_id("inexistente") is None
        assert svc.resolve_agent_id("inexistente") is None
        assert perguntas == ["inexistente"]

    def test_negativa_expira_no_ttl(self, monkeypatch):
        """O vinculo recem-criado no painel passa a valer sozinho."""
        efetivos = {}
        svc, perguntas = _service_agentes(monkeypatch, efetivos)
        # TTL ja' vencido: a negativa e' gravada, mas nao segura a proxima.
        monkeypatch.setattr(type(svc), "_NEGATIVE_TTL", -1)
        assert svc.resolve_agent_id("novo") is None

        efetivos["novo"] = {"agent_id": AG_ETAPA, "model_id": MODELO}
        assert svc.resolve_agent_id("novo") == AG_ETAPA
        assert perguntas == ["novo", "novo"]

    def test_invalidar_cache_limpa_negativa(self, monkeypatch):
        efetivos = {}
        svc, _ = _service_agentes(monkeypatch, efetivos)
        assert svc.resolve_agent_id("novo") is None
        efetivos["novo"] = {"agent_id": AG_ETAPA, "model_id": MODELO}
        svc.invalidate_effective_model_cache("novo")
        assert svc.resolve_agent_id("novo") == AG_ETAPA

    def test_slug_vazio_nao_toca_a_rede(self, monkeypatch):
        svc, perguntas = _service_agentes(monkeypatch, {})
        assert svc.resolve_agent_id("") is None
        assert perguntas == []


class TestDimensoesNoPayload:
    def _payload(self, monkeypatch, efetivos, **kw):
        svc, _ = _service_agentes(monkeypatch, efetivos)
        assert svc.track_token_usage(model_name="gpt-4o", prompt_tokens=10,
                                     completion_tokens=5, **kw) is True
        return svc._queue.get_nowait()[1]

    def test_slugs_viram_colunas(self, monkeypatch):
        p = self._payload(
            monkeypatch,
            {"sql": {"agent_id": AG_ETAPA}, "vendas": {"agent_id": AG_AREA}},
            agent_slug="sql", area_agent_slug="vendas",
        )
        assert p["agent_id"] == AG_ETAPA
        assert p["area_agent_id"] == AG_AREA

    def test_sem_etapa_a_etapa_e_a_area(self, monkeypatch):
        """Quem so marcou o agente na entrada nao perde atribuicao."""
        p = self._payload(monkeypatch, {"vendas": {"agent_id": AG_AREA}},
                          area_agent_slug="vendas")
        assert p["agent_id"] == AG_AREA
        assert p["area_agent_id"] == AG_AREA

    def test_id_explicito_vence_o_slug(self, monkeypatch):
        p = self._payload(monkeypatch, {"sql": {"agent_id": AG_ETAPA}},
                          agent_slug="sql", agent_id=AG_AREA)
        assert p["agent_id"] == AG_AREA

    def test_slug_que_nao_resolve_omite_a_coluna(self, monkeypatch):
        """As colunas sao FK para agents.id — id inventado derrubaria o lote."""
        p = self._payload(monkeypatch, {}, agent_slug="fantasma")
        assert "agent_id" not in p
        assert "area_agent_id" not in p

    def test_slug_sobrevive_no_metadata_mesmo_sem_id(self, monkeypatch):
        """E o que permite ler o rastro quando o agente nao esta vinculado."""
        p = self._payload(monkeypatch, {}, agent_slug="fantasma")
        assert p["alert_metadata"]["agent_slug"] == "fantasma"

    def test_sem_agente_nenhum_nao_manda_dimensao(self, monkeypatch):
        p = self._payload(monkeypatch, {})
        assert "agent_id" not in p and "area_agent_id" not in p
        assert "agent_slug" not in p["alert_metadata"]


class TestHerancaDoContextVar:
    """`definir_agente()` na entrada do endpoint deve alcancar o payload."""

    @pytest.fixture(autouse=True)
    def _limpa_agente(self):
        from automaxia_utils import definir_agente
        definir_agente(None)
        yield
        definir_agente(None)

    def _chamada(self, monkeypatch, **kw):
        from automaxia_utils.token_tracking import counter as mod
        capturado = {}

        class _FakeAdmin:
            config = type("C", (), {"enabled": True})()

            def track_token_usage(self, **payload):
                capturado.update(payload)
                return True

        fake = _FakeAdmin()
        monkeypatch.setattr(mod, "get_admin_center_service", lambda: fake)
        contador = mod.HybridTokenCounter("gpt-4o")
        contador.admin_center = fake
        monkeypatch.setattr(contador, "calculate_costs", lambda *a, **k: {
            "cost_usd": 0.0, "cost_brl": 0.0, "exchange_rate": 5.0,
            "price_source": "test", "cost_breakdown": {},
        })
        monkeypatch.setattr(mod, "HybridTokenCounter", lambda *a, **k: contador)
        mod.track_api_response(
            {"usage": {"prompt_tokens": 10, "completion_tokens": 5,
                       "total_tokens": 15}},
            model="gpt-4o", prompt_text="oi", **kw
        )
        return capturado

    def test_area_marcada_na_entrada_chega_ao_payload(self, monkeypatch):
        from automaxia_utils import definir_agente
        definir_agente("vendas")
        p = self._chamada(monkeypatch)
        assert p["area_agent_slug"] == "vendas"
        assert p["agent_slug"] == "vendas"

    def test_etapa_explicita_nao_apaga_a_area(self, monkeypatch):
        """Uma pergunta dispara varias etapas: o custo de cada trecho vai para o
        seu dono, mas a requisicao continua sendo da area."""
        from automaxia_utils import definir_agente
        definir_agente("vendas")
        p = self._chamada(monkeypatch, agent_slug="sql-generator")
        assert p["agent_slug"] == "sql-generator"
        assert p["area_agent_slug"] == "vendas"

    def test_sem_definir_agente_nao_inventa_area(self, monkeypatch):
        p = self._chamada(monkeypatch, agent_slug="sql-generator")
        assert p["agent_slug"] == "sql-generator"
        assert p["area_agent_slug"] is None
