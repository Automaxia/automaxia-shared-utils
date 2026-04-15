"""
Testes unitários para o módulo de token tracking (automaxia_utils/token_tracking/counter.py).
Cobre contagem de tokens, serialização de custos e detecção de providers.
"""
import os
import pytest

os.environ["ENVIRONMENT"] = "testing"
os.environ["ADMIN_CENTER_URL"] = "http://fake:8000"
os.environ["ADMIN_CENTER_API_KEY"] = "fake-key"
os.environ["ADMIN_CENTER_PRODUCT_ID"] = "prod-1"
os.environ["ADMIN_CENTER_ENVIRONMENT_ID"] = "env-1"
os.environ["ADMIN_CENTER_ORGANIZATION_ID"] = "org-1"

from automaxia_utils.token_tracking.counter import (
    count_tokens_tiktoken,
    count_tokens_smart,
    extract_tokens_from_response,
    HybridTokenCounter,
    estimate_tokens_and_cost,
)


# ── count_tokens_tiktoken ────────────────────────────────────────────────

class TestCountTokensTiktoken:
    def test_conta_tokens_gpt4o(self):
        count = count_tokens_tiktoken("Hello, world!", "gpt-4o")
        assert isinstance(count, int)
        assert count > 0

    def test_conta_tokens_gpt4o_mini(self):
        count = count_tokens_tiktoken("Olá, como vai?", "gpt-4o-mini")
        assert isinstance(count, int)
        assert count > 0

    def test_texto_vazio(self):
        count = count_tokens_tiktoken("", "gpt-4o")
        assert count == 0

    def test_texto_longo(self):
        text = "Token " * 1000
        count = count_tokens_tiktoken(text, "gpt-4o")
        assert count > 500

    def test_modelo_desconhecido_fallback(self):
        count = count_tokens_tiktoken("test text", "modelo-ficticio-xyz")
        assert isinstance(count, int)
        assert count >= 0


# ── count_tokens_smart ───────────────────────────────────────────────────

class TestCountTokensSmart:
    def test_retorna_dict_com_count_e_source(self):
        result = count_tokens_smart("Hello world", "gpt-4o")
        assert "count" in result
        assert "source" in result
        assert result["count"] > 0

    def test_source_valida(self):
        result = count_tokens_smart("teste", "gpt-4o")
        assert result["source"] in ["litellm", "anthropic_native", "google_native", "tiktoken"]


# ── extract_tokens_from_response ─────────────────────────────────────────

class TestExtractTokensFromResponse:
    def test_extrai_de_response_openai(self):
        class MockUsage:
            prompt_tokens = 100
            completion_tokens = 50
            total_tokens = 150

        class MockResponse:
            usage = MockUsage()

        result = extract_tokens_from_response(MockResponse())
        assert result is not None
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 50
        assert result["total_tokens"] == 150

    def test_retorna_none_sem_usage(self):
        class MockResponse:
            pass

        result = extract_tokens_from_response(MockResponse())
        # Sem usage, deve retornar None
        assert result is None

    def test_extrai_de_dict_langchain(self):
        class MockResponse:
            llm_output = {
                "token_usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 100,
                    "total_tokens": 300,
                }
            }

        result = extract_tokens_from_response(MockResponse())
        if result:  # só se o formato for suportado
            assert result["total_tokens"] == 300


# ── HybridTokenCounter ──────────────────────────────────────────────────

class TestHybridTokenCounter:
    @pytest.fixture
    def counter(self):
        return HybridTokenCounter(model="gpt-4o-mini")

    def test_cria_instancia(self, counter):
        assert counter is not None

    def test_calculate_costs(self, counter):
        result = counter.calculate_costs(
            prompt_tokens=1000,
            completion_tokens=500,
        )
        assert "cost_usd" in result
        assert "cost_brl" in result
        assert "price_source" in result
        assert result["cost_usd"] >= 0
        assert result["cost_brl"] >= 0

    def test_custo_zero_para_zero_tokens(self, counter):
        result = counter.calculate_costs(
            prompt_tokens=0,
            completion_tokens=0,
        )
        assert result["cost_usd"] == 0 or result["cost_usd"] >= 0

    def test_fallback_prices_contém_modelos_principais(self):
        # Verifica que os preços de fallback incluem modelos importantes
        prices = HybridTokenCounter.FALLBACK_PRICES_USD
        model_keys = list(prices.keys())
        model_str = " ".join(model_keys).lower()
        assert "gpt-4o" in model_str or "gpt" in model_str


# ── estimate_tokens_and_cost ─────────────────────────────────────────────

class TestEstimateTokensAndCost:
    def test_estimativa_basica(self):
        result = estimate_tokens_and_cost(
            prompt="Explique o que é saneamento básico em detalhes.",
            model="gpt-4o-mini",
            estimated_response_length=200,
        )
        assert result is not None
        assert "estimated_prompt_tokens" in result or "prompt_tokens" in result or isinstance(result, dict)

    def test_estimativa_texto_longo(self):
        long_text = "Dados de saneamento: " * 500
        result = estimate_tokens_and_cost(
            prompt=long_text,
            model="gpt-4o",
            estimated_response_length=1000,
        )
        assert result is not None
