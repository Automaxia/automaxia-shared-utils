"""Testes das funcionalidades trazidas do `infrabalance-shared-utils` (1.14.0).

Cada bloco cobre um comportamento que ANTES falhava em silencio — que e' o
motivo de cada porte. Ver CHANGELOG 1.14.0.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from automaxia_utils.admin_center.connections import ResolvedConnection
from automaxia_utils.admin_center.service import AdminCenterConfig, AdminCenterService
from automaxia_utils.token_tracking.counter import extract_tokens_from_response


# ── ambiente limpo entre testes ─────────────────────────────────────────

VARS = [
    "ENVIRONMENT", "ADMIN_CENTER_URL", "ADMIN_CENTER_URL_LOCAL",
    "ADMIN_CENTER_DEV_URL", "ADMIN_CENTER_PRODUCT_SLUG", "PRODUCT_SLUG",
    "ADMIN_CENTER_LOG_MIN_LEVEL", "SECRET_KEY", "JWT_SECRET_KEY",
]


@pytest.fixture(autouse=True)
def _env_limpo():
    antigo = {k: os.environ.get(k) for k in VARS}
    for k in VARS:
        os.environ.pop(k, None)
    yield
    for k, v in antigo.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v


# ── ResolvedConnection multi-engine ─────────────────────────────────────

def _payload_base(**extra):
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "alias": "cli",
        "engine": "postgresql",
        "schema_name": "public",
        "username": "u",
        "password": "p",
        "use_tunnel": False,
        "version": 1,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    base.update(extra)
    return base


class TestResolvedConnectionEngines:
    def test_engine_rest_sem_host_port_database(self):
        """Antes levantava KeyError em data["host"] — o produto via
        'conexao nao encontrada' para uma conexao que existia."""
        rc = ResolvedConnection.from_dict(_payload_base(
            engine="rest", username="", base_url="https://api.cliente.com",
            auth_type="bearer",
        ))
        assert rc.engine == "rest"
        assert rc.host is None and rc.port is None and rc.database_name is None
        assert rc.base_url == "https://api.cliente.com"
        assert rc.auth_type == "bearer"

    def test_engine_databricks_carrega_http_path(self):
        rc = ResolvedConnection.from_dict(_payload_base(
            engine="databricks", host="ws.databricks.com",
            databricks_http_path="/sql/1.0/warehouses/abc", username="",
        ))
        assert rc.databricks_http_path == "/sql/1.0/warehouses/abc"

    def test_campos_semanticos_do_cofre_chegam_ao_dto(self):
        """Satelites leem via getattr — campo ausente degrada em silencio."""
        rc = ResolvedConnection.from_dict(_payload_base(
            events_table="gold.eventos", operational_table="gold.operacional",
            ai_context="Base de vendas", arcgis_config={"layer": 3},
        ))
        assert rc.events_table == "gold.eventos"
        assert rc.operational_table == "gold.operacional"
        assert rc.ai_context == "Base de vendas"
        assert rc.arcgis_config == {"layer": 3}

    def test_dsn_sql_continua_funcionando(self):
        rc = ResolvedConnection.from_dict(_payload_base(
            host="db", port=5432, database_name="app",
        ))
        assert rc.dsn() == "postgresql+psycopg2://u:p@db:5432/app"

    @pytest.mark.parametrize("engine", ["rest", "arcgis", "databricks"])
    def test_dsn_recusa_engine_sem_dsn(self, engine):
        rc = ResolvedConnection.from_dict(_payload_base(engine=engine, username=""))
        with pytest.raises(ValueError, match="nao tem DSN SQL"):
            rc.dsn()

    def test_dsn_recusa_sql_incompleto(self):
        rc = ResolvedConnection.from_dict(_payload_base(engine="mysql"))
        with pytest.raises(ValueError, match="exige host, port e database_name"):
            rc.dsn()


# ── AdminCenterAuthConfig.from_env ──────────────────────────────────────

class TestAuthConfigFromEnv:
    def _config(self):
        from automaxia_utils.auth.middleware import AdminCenterAuthConfig
        return AdminCenterAuthConfig.from_env()

    def test_aceita_admin_center_product_slug(self):
        """Os satelites do Studio definem ADMIN_CENTER_PRODUCT_SLUG; lendo so'
        PRODUCT_SLUG o gate de produto era PULADO em silencio."""
        os.environ["ADMIN_CENTER_PRODUCT_SLUG"] = "talk"
        assert self._config().product_slug == "talk"

    def test_product_slug_legado_ainda_vale(self):
        os.environ["PRODUCT_SLUG"] = "vision"
        assert self._config().product_slug == "vision"

    def test_prefixada_vence_a_legada(self):
        os.environ["ADMIN_CENTER_PRODUCT_SLUG"] = "talk"
        os.environ["PRODUCT_SLUG"] = "vision"
        assert self._config().product_slug == "talk"

    def test_dev_usa_url_local(self):
        """Sem isso o /auth/me/full ia para PRODUCAO com token do ambiente
        local e todo require_permission respondia 403/503."""
        os.environ["ENVIRONMENT"] = "development"
        os.environ["ADMIN_CENTER_URL"] = "https://prod.example.com/api"
        os.environ["ADMIN_CENTER_URL_LOCAL"] = "http://127.0.0.1:8002/api"
        assert self._config().admincenter_url == "http://127.0.0.1:8002/api"

    def test_dev_url_tem_prioridade_sobre_url_local(self):
        os.environ["ENVIRONMENT"] = "development"
        os.environ["ADMIN_CENTER_URL"] = "https://prod.example.com/api"
        os.environ["ADMIN_CENTER_DEV_URL"] = "http://dev:8002/api"
        os.environ["ADMIN_CENTER_URL_LOCAL"] = "http://127.0.0.1:8002/api"
        assert self._config().admincenter_url == "http://dev:8002/api"

    def test_producao_ignora_url_local(self):
        os.environ["ENVIRONMENT"] = "production"
        os.environ["ADMIN_CENTER_URL"] = "https://prod.example.com/api"
        os.environ["ADMIN_CENTER_URL_LOCAL"] = "http://127.0.0.1:8002/api"
        assert self._config().admincenter_url == "https://prod.example.com/api"


# ── extracao de tokens da LangChain ─────────────────────────────────────

class _AIMessage:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class TestExtracaoLangChain:
    def test_response_metadata_formato_openai(self):
        """llm.invoke() devolve AIMessage; sem este caminho o tracking caia na
        estimativa por tiktoken mesmo tendo o numero exato."""
        msg = _AIMessage(response_metadata={
            "token_usage": {"prompt_tokens": 120, "completion_tokens": 30,
                            "total_tokens": 150}
        })
        assert extract_tokens_from_response(msg) == {
            "prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "reasoning_tokens": 0,
        }

    def test_usage_metadata_padrao_langchain(self):
        msg = _AIMessage(usage_metadata={"input_tokens": 10, "output_tokens": 5,
                                         "total_tokens": 15})
        tokens = extract_tokens_from_response(msg)
        assert tokens["prompt_tokens"] == 10
        assert tokens["completion_tokens"] == 5

    def test_usage_direto_ainda_tem_prioridade(self):
        msg = _AIMessage(
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            usage_metadata={"input_tokens": 999, "output_tokens": 999},
        )
        assert extract_tokens_from_response(msg)["prompt_tokens"] == 1

    def test_sem_nada_devolve_none(self):
        assert extract_tokens_from_response(_AIMessage()) is None


# ── filtro de nivel + decomposicao de context ───────────────────────────

def _service(**cfg):
    """Service sem rede: enabled=False no construtor evita o _initialize."""
    svc = AdminCenterService(AdminCenterConfig(enabled=False, **cfg))
    svc.config.enabled = True
    return svc


class TestFiltroDeNivel:
    def test_default_e_warning(self):
        os.environ["ADMIN_CENTER_URL"] = "http://x"
        os.environ["ADMIN_CENTER_API_KEY"] = "k"
        assert AdminCenterConfig.from_env().log_min_level == "WARNING"

    def test_info_nao_persiste_no_default(self):
        svc = _service()
        assert svc._nivel_persistivel("INFO") is False
        assert svc._nivel_persistivel("DEBUG") is False

    def test_warning_e_acima_persistem(self):
        svc = _service()
        for nivel in ("WARNING", "ERROR", "CRITICAL"):
            assert svc._nivel_persistivel(nivel) is True

    def test_min_level_configuravel(self):
        svc = _service(log_min_level="INFO")
        assert svc._nivel_persistivel("INFO") is True
        assert svc._nivel_persistivel("DEBUG") is False

    def test_nivel_desconhecido_passa(self):
        """Custo do erro e' assimetrico: melhor gravar a mais do que engolir."""
        assert _service()._nivel_persistivel("AUDIT") is True

    def test_log_application_descarta_antes_da_fila(self):
        svc = _service(product_id="p", environment_id="e")
        assert svc.log_application("INFO", "ruido") is False
        assert svc._queue.empty()


class TestGuardaDeIdentidade:
    def test_sem_identidade_nao_enfileira(self):
        """Sem product_id/environment_id o payload leva '' onde o contrato quer
        UUID — 422 do outro lado, silencioso."""
        svc = _service()
        assert svc.log_application("ERROR", "x") is False
        assert svc.log_execution("/a", "GET", 200, 5) is False
        assert svc.log_process("p", "started") is False
        assert svc._queue.empty()

    def test_com_identidade_enfileira(self):
        svc = _service(product_id="p", environment_id="e")
        assert svc.log_application("ERROR", "x") is True
        assert svc._queue.qsize() == 1


class TestDecomposicaoDeContext:
    def _payload(self, **kw):
        svc = _service(product_id="p", environment_id="e")
        assert svc.log_application("ERROR", "boom", **kw) is True
        return svc._queue.get_nowait()[1]

    def test_nomes_do_logging_viram_colunas(self):
        """`ApplicationLogPOST` nao tem campo `context` — o Pydantic descartava
        o dict inteiro sem reclamar."""
        p = self._payload(context={"logger": "app.svc", "module": "svc",
                                   "funcName": "roda", "lineno": 42})
        assert p["logger_name"] == "app.svc"
        assert p["module_name"] == "svc"
        assert p["function_name"] == "roda"
        assert p["line_number"] == 42
        assert p["extra_data"] == {}

    def test_apelidos_curtos_do_talk_api(self):
        """O interceptor do talk-api monta {'module','function','line','thread'}."""
        p = self._payload(context={"module": "ai", "function": "gerar",
                                   "line": 120, "thread": 7})
        assert p["module_name"] == "ai"
        assert p["function_name"] == "gerar"
        assert p["line_number"] == 120
        assert p["extra_data"] == {"thread": 7}

    def test_o_que_nao_e_coluna_vai_para_extra_data(self):
        p = self._payload(context={"logger": "app", "sql": "select 1",
                                   "pergunta": "quantos?"})
        assert p["extra_data"] == {"sql": "select 1", "pergunta": "quantos?"}
        assert p["logger_name"] == "app"

    def test_parametro_explicito_vence_o_context(self):
        p = self._payload(logger_name="explicito", context={"logger": "do-context"})
        assert p["logger_name"] == "explicito"
        assert p["extra_data"] == {}

    def test_line_number_nao_numerico_nao_derruba_o_lote(self):
        p = self._payload(context={"lineno": "abc"})
        assert "line_number" not in p
        assert p["extra_data"]["line_number"] == "abc"

    def test_sem_context_payload_continua_minimo(self):
        p = self._payload()
        assert p["extra_data"] == {}
        assert "logger_name" not in p


class TestCorrelacaoDeExecucao:
    def _payload(self, **kw):
        svc = _service(product_id="11111111-1111-1111-1111-111111111111",
                       environment_id="22222222-2222-2222-2222-222222222222")
        assert svc.log_process("carga", "started", **kw) is True
        return svc._queue.get_nowait()[1]

    def test_execution_id_vira_request_id(self):
        rid = "33333333-3333-3333-3333-333333333333"
        assert self._payload(execution_id=rid)["request_id"] == rid

    def test_execution_id_invalido_e_descartado_sem_derrubar(self):
        assert "request_id" not in self._payload(execution_id="nao-e-uuid")

    def test_sem_execution_id_nao_manda_request_id(self):
        assert "request_id" not in self._payload()


# ── config: product_slug + endpoints de descoberta ──────────────────────

class TestProductSlugNaConfig:
    def test_from_env_le_slug_prefixado(self):
        os.environ["ADMIN_CENTER_URL"] = "http://x"
        os.environ["ADMIN_CENTER_API_KEY"] = "k"
        os.environ["ADMIN_CENTER_PRODUCT_SLUG"] = "talk"
        assert AdminCenterConfig.from_env().product_slug == "talk"

    def test_from_env_aceita_slug_raiz(self):
        os.environ["ADMIN_CENTER_URL"] = "http://x"
        os.environ["ADMIN_CENTER_API_KEY"] = "k"
        os.environ["PRODUCT_SLUG"] = "harvest"
        assert AdminCenterConfig.from_env().product_slug == "harvest"

    def test_get_variable_sem_environment_id_nao_chama_none(self, monkeypatch):
        """Antes montava /environment/None/variables e devolvia 404."""
        svc = _service()
        chamadas = []
        monkeypatch.setattr(svc, "_make_request",
                            lambda *a, **k: chamadas.append(a) or None)
        assert svc.get_variable() is None
        assert chamadas == []

    def test_endpoints_de_descoberta_exportados(self):
        from automaxia_utils import AdminCenterEndpoints
        assert AdminCenterEndpoints.PRODUCT_BY_SLUG == "/product/consulta_slug"
        assert (AdminCenterEndpoints.PRODUCT_ENVIRONMENTS.format("abc")
                == "/product/abc/environment")
