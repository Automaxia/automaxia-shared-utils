"""
Modulo hibrido para contagem de tokens - Arquitetura multi-nivel
v1.1.0 - LiteLLM universal + APIs nativas + tiktoken fallback

Hierarquia de precisao:
  1. response.usage (da API) -> 100% exato
  2. LiteLLM token_counter -> universal, 100+ modelos
  3. APIs nativas (Anthropic/Google) -> exato por provider
  4. tiktoken -> fallback offline (preciso para OpenAI)
  5. len(text) // 4 -> ultimo recurso
"""
import logging
import requests
from typing import Dict, Any, List, Optional, Union
from datetime import datetime, timedelta
from decimal import Decimal
import tiktoken

# ============================================
# IMPORTS OPCIONAIS
# ============================================

# LiteLLM - contagem universal de tokens e custos
try:
    import litellm
    from litellm import token_counter as litellm_token_counter
    from litellm import cost_per_token as litellm_cost_per_token
    from litellm import completion_cost as litellm_completion_cost
    LITELLM_AVAILABLE = True
except ImportError:
    LITELLM_AVAILABLE = False
    logging.warning("LiteLLM nao disponivel. Usando tiktoken como fallback.")

# Anthropic - contagem nativa
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# Google Generative AI - contagem nativa
try:
    import google.generativeai as genai
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

# LangChain
try:
    from langchain.callbacks.base import BaseCallbackHandler
    try:
        from langchain_community.callbacks.manager import get_openai_callback
    except ImportError:
        from langchain.callbacks import get_openai_callback
    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False

from automaxia_utils.admin_center.service import get_admin_center_service


# ============================================
# SERVICO DE COTACAO
# ============================================

class CurrencyService:
    """Servico para obter cotacao USD/BRL atualizada"""

    def __init__(self):
        self._cached_rate = None
        self._cache_timestamp = None
        self._cache_duration = timedelta(minutes=30)

    def get_usd_to_brl_rate(self) -> float:
        now = datetime.now()

        if (self._cached_rate and self._cache_timestamp and
            now - self._cache_timestamp < self._cache_duration):
            return self._cached_rate

        try:
            response = requests.get(
                "https://api.exchangerate-api.com/v4/latest/USD",
                timeout=5
            )
            response.raise_for_status()
            rate = response.json()['rates']['BRL']
            self._cached_rate = rate
            self._cache_timestamp = now
            return rate
        except Exception as e:
            logging.warning(f"Erro ao obter cotacao USD/BRL: {e}")
            if not self._cached_rate:
                self._cached_rate = 5.0
            return self._cached_rate

currency_service = CurrencyService()


# ============================================
# CONTAGEM DE TOKENS - MULTI-NIVEL
# ============================================

def count_tokens_litellm(text: str, model: str) -> Optional[int]:
    """Nivel 2: Contagem via LiteLLM (universal, 100+ modelos)"""
    if not LITELLM_AVAILABLE:
        return None
    try:
        messages = [{"role": "user", "content": text}]
        count = litellm_token_counter(model=model, messages=messages)
        logging.debug(f"LiteLLM token count para '{model}': {count}")
        return count
    except Exception as e:
        logging.debug(f"LiteLLM token count falhou para '{model}': {e}")
        return None


def count_tokens_anthropic_native(text: str, model: str) -> Optional[int]:
    """Nivel 3: Contagem nativa Anthropic (exato para Claude)"""
    if not ANTHROPIC_AVAILABLE:
        return None
    if "claude" not in model.lower():
        return None
    try:
        client = anthropic.Anthropic()
        count = client.count_tokens(text)
        logging.debug(f"Anthropic native count para '{model}': {count}")
        return count
    except Exception as e:
        logging.debug(f"Anthropic native count falhou: {e}")
        return None


def count_tokens_google_native(text: str, model: str) -> Optional[int]:
    """Nivel 3: Contagem nativa Google (exato para Gemini)"""
    if not GOOGLE_AVAILABLE:
        return None
    if "gemini" not in model.lower():
        return None
    try:
        gmodel = genai.GenerativeModel(model)
        result = gmodel.count_tokens(text)
        count = result.total_tokens
        logging.debug(f"Google native count para '{model}': {count}")
        return count
    except Exception as e:
        logging.debug(f"Google native count falhou: {e}")
        return None


def count_tokens_tiktoken(text: str, model: str = "gpt-3.5-turbo") -> int:
    """Nivel 4: Contagem via tiktoken (fallback offline, preciso para OpenAI)"""
    try:
        encoding = tiktoken.encoding_for_model(model)
        return len(encoding.encode(text))
    except Exception:
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text))
        except Exception as e:
            logging.warning(f"tiktoken falhou: {e}")
            return len(text) // 4


def count_tokens_smart(text: str, model: str) -> Dict[str, Any]:
    """
    Contagem inteligente multi-nivel. Tenta cada metodo em ordem de precisao.

    Returns:
        Dict com 'count' (int) e 'source' (str indicando metodo usado)
    """
    # Nivel 2: LiteLLM (universal)
    count = count_tokens_litellm(text, model)
    if count is not None:
        return {"count": count, "source": "litellm"}

    # Nivel 3: APIs nativas
    count = count_tokens_anthropic_native(text, model)
    if count is not None:
        return {"count": count, "source": "anthropic_native"}

    count = count_tokens_google_native(text, model)
    if count is not None:
        return {"count": count, "source": "google_native"}

    # Nivel 4: tiktoken
    count = count_tokens_tiktoken(text, model)
    return {"count": count, "source": "tiktoken"}


# ============================================
# EXTRACAO DE TOKENS DA RESPOSTA
# ============================================

def extract_tokens_from_response(response: Any) -> Optional[Dict[str, int]]:
    """
    Nivel 1: Extrai tokens REAIS da resposta da API (mais preciso possivel)
    """
    try:
        # OpenAI SDK v1.x+
        if hasattr(response, 'usage'):
            usage = response.usage
            if hasattr(usage, 'prompt_tokens'):
                return {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens
                }
            # Anthropic
            if hasattr(usage, 'input_tokens'):
                return {
                    "prompt_tokens": usage.input_tokens,
                    "completion_tokens": usage.output_tokens,
                    "total_tokens": usage.input_tokens + usage.output_tokens
                }

        # LangChain
        if hasattr(response, 'llm_output') and isinstance(response.llm_output, dict):
            if 'token_usage' in response.llm_output:
                usage = response.llm_output['token_usage']
                return {
                    "prompt_tokens": usage.get('prompt_tokens', 0),
                    "completion_tokens": usage.get('completion_tokens', 0),
                    "total_tokens": usage.get('total_tokens', 0)
                }

        # Dict format
        if isinstance(response, dict) and 'usage' in response:
            usage = response['usage']
            if 'prompt_tokens' in usage:
                return {
                    "prompt_tokens": usage['prompt_tokens'],
                    "completion_tokens": usage['completion_tokens'],
                    "total_tokens": usage['total_tokens']
                }

        return None

    except Exception as e:
        logging.error(f"Erro ao extrair tokens: {e}")
        return None


# ============================================
# CALCULO DE CUSTOS - MULTI-NIVEL
# ============================================

class HybridTokenCounter:
    """
    Contador hibrido com custos via LiteLLM + API AdminCenter + fallback
    """

    # Precos fallback atualizados (Mar 2026) por 1K tokens em USD
    FALLBACK_PRICES_USD = {
        "gpt-4o": {"input": 0.0025, "output": 0.01},
        "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
        "gpt-4-turbo": {"input": 0.01, "output": 0.03},
        "gpt-4": {"input": 0.03, "output": 0.06},
        "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
        "claude-sonnet-4-20250514": {"input": 0.003, "output": 0.015},
        "claude-3-5-sonnet-20241022": {"input": 0.003, "output": 0.015},
        "claude-3-5-haiku-20241022": {"input": 0.0008, "output": 0.004},
        "claude-3-opus-20240229": {"input": 0.015, "output": 0.075},
        "gemini-1.5-pro": {"input": 0.00125, "output": 0.005},
        "gemini-1.5-flash": {"input": 0.000075, "output": 0.0003},
        "gemini-2.0-flash": {"input": 0.0001, "output": 0.0004},
    }

    def __init__(self, model: str = "gpt-3.5-turbo"):
        self.model = model
        self.admin_center = get_admin_center_service()
        self._price_cache = {}

    def calculate_costs(self, prompt_tokens: int, completion_tokens: int) -> Dict[str, Any]:
        """Calcula custos em USD e BRL"""

        # Nivel 1: LiteLLM (precos atualizados automaticamente)
        costs_litellm = self._calculate_via_litellm(prompt_tokens, completion_tokens)
        if costs_litellm:
            return costs_litellm

        # Nivel 2: API AdminCenter
        costs_api = self._calculate_via_api(prompt_tokens, completion_tokens)
        if costs_api:
            return costs_api

        # Nivel 3: Fallback hardcoded
        return self._calculate_via_fallback(prompt_tokens, completion_tokens)

    def _calculate_via_litellm(self, prompt_tokens: int, completion_tokens: int) -> Optional[Dict]:
        """Custos via LiteLLM (mantido atualizado pela comunidade)"""
        if not LITELLM_AVAILABLE:
            return None
        try:
            prompt_cost, completion_cost = litellm_cost_per_token(
                model=self.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens
            )
            total_usd = prompt_cost + completion_cost
            exchange_rate = currency_service.get_usd_to_brl_rate()

            return {
                "cost_usd": round(total_usd, 6),
                "cost_brl": round(total_usd * exchange_rate, 4),
                "exchange_rate": exchange_rate,
                "price_source": "litellm",
                "cost_breakdown": {
                    "input_usd": round(prompt_cost, 6),
                    "output_usd": round(completion_cost, 6),
                    "input_brl": round(prompt_cost * exchange_rate, 4),
                    "output_brl": round(completion_cost * exchange_rate, 4)
                }
            }
        except Exception as e:
            logging.debug(f"LiteLLM cost falhou para '{self.model}': {e}")
            return None

    def _calculate_via_api(self, prompt_tokens: int, completion_tokens: int) -> Optional[Dict]:
        """Custos via API AdminCenter"""
        try:
            if not self.admin_center.config.enabled:
                return None

            # Cache de 1 hora
            cache_key = self.model
            if cache_key in self._price_cache:
                cached = self._price_cache[cache_key]
                if datetime.now() - cached["timestamp"] < timedelta(hours=1):
                    prices = cached["prices"]
                    return self._build_cost_result(prompt_tokens, completion_tokens, prices, "admin_center_api")

            params = {"name": self.model}
            response = self.admin_center._make_request("GET", "/ai-model/consulta_nome", params=params)

            if response and "data" in response:
                model_data = response["data"]
                input_cost = model_data.get("input_cost_per_token")
                output_cost = model_data.get("output_cost_per_token")

                if input_cost is not None and output_cost is not None:
                    prices = {"input": float(input_cost) * 1000, "output": float(output_cost) * 1000}
                    self._price_cache[cache_key] = {"prices": prices, "timestamp": datetime.now()}
                    return self._build_cost_result(prompt_tokens, completion_tokens, prices, "admin_center_api")

            return None
        except Exception as e:
            logging.debug(f"API cost falhou para '{self.model}': {e}")
            return None

    def _calculate_via_fallback(self, prompt_tokens: int, completion_tokens: int) -> Dict:
        """Custos via precos hardcoded (ultimo recurso)"""
        fallback_key = self.model if self.model in self.FALLBACK_PRICES_USD else "gpt-3.5-turbo"

        # Tentar match parcial
        if fallback_key not in self.FALLBACK_PRICES_USD:
            for key in self.FALLBACK_PRICES_USD:
                if key in self.model or self.model in key:
                    fallback_key = key
                    break

        prices = self.FALLBACK_PRICES_USD.get(fallback_key, {"input": 0.001, "output": 0.002})
        return self._build_cost_result(prompt_tokens, completion_tokens, prices, "fallback_hardcoded")

    def _build_cost_result(self, prompt_tokens: int, completion_tokens: int,
                           prices: Dict[str, float], source: str) -> Dict:
        cost_input_usd = (prompt_tokens / 1000) * prices["input"]
        cost_output_usd = (completion_tokens / 1000) * prices["output"]
        total_cost_usd = cost_input_usd + cost_output_usd
        exchange_rate = currency_service.get_usd_to_brl_rate()

        return {
            "cost_usd": round(total_cost_usd, 6),
            "cost_brl": round(total_cost_usd * exchange_rate, 4),
            "exchange_rate": exchange_rate,
            "price_source": source,
            "cost_breakdown": {
                "input_usd": round(cost_input_usd, 6),
                "output_usd": round(cost_output_usd, 6),
                "input_brl": round(cost_input_usd * exchange_rate, 4),
                "output_brl": round(cost_output_usd * exchange_rate, 4)
            }
        }


# ============================================
# FUNCAO PRINCIPAL (RECOMENDADA)
# ============================================

def track_api_response(
    response: Any,
    model: str,
    endpoint: str = "/api_direct",
    user_id: Optional[str] = None,
    prompt_text: str = "",
    prompt_id: Optional[str] = None,
    force_provider: Optional[str] = None
) -> Dict[str, Any]:
    """
    FUNCAO UNIVERSAL: Detecta automaticamente OpenAI, LangChain, Anthropic, Google, etc.

    Hierarquia de precisao:
      1. response.usage (da API) -> 100% exato
      2. LiteLLM -> universal, 100+ modelos
      3. APIs nativas (Anthropic/Google) -> exato por provider
      4. tiktoken -> fallback offline
    """
    counter = HybridTokenCounter(model)

    # 1. DETECCAO DO PROVIDER
    provider = force_provider or _detect_provider(response)

    # 2. EXTRACAO DE TOKENS (Nivel 1: response.usage)
    tokens = None
    source = "unknown"

    if provider == "openai":
        tokens = _extract_openai_tokens(response)
        source = "openai_api" if tokens else None
    elif provider == "langchain":
        tokens = _extract_langchain_tokens(response)
        source = "langchain_api" if tokens else None
    elif provider == "anthropic":
        tokens = _extract_anthropic_tokens(response)
        source = "anthropic_api" if tokens else None
    elif provider == "google":
        tokens = _extract_google_tokens(response)
        source = "google_api" if tokens else None
    else:
        tokens = extract_tokens_from_response(response)
        source = "generic_extraction" if tokens else None

    # 3. FALLBACK MULTI-NIVEL (se response.usage nao disponivel)
    if not tokens:
        # Tentar contagem inteligente do prompt
        smart_result = count_tokens_smart(prompt_text, model)
        prompt_tokens = smart_result["count"]
        source = smart_result["source"] + "_fallback"

        # Tentar extrair texto da resposta e contar
        response_text = _extract_response_text(response, provider)
        if response_text:
            smart_completion = count_tokens_smart(response_text, model)
            completion_tokens = smart_completion["count"]
        else:
            completion_tokens = 0

        tokens = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        }

    prompt_tokens = tokens["prompt_tokens"]
    completion_tokens = tokens["completion_tokens"]

    logging.info(
        f"Tokens via {source} (provider: {provider}): "
        f"prompt={prompt_tokens}, completion={completion_tokens}"
    )

    # 4. CALCULAR CUSTOS (multi-nivel: LiteLLM -> API -> fallback)
    costs = counter.calculate_costs(prompt_tokens, completion_tokens)

    # 5. METADATA
    enhanced_metadata = {
        "prompt_text": prompt_text[:500] if prompt_text else "",
        "model_name": model,
        "provider": provider,
        "token_source": source,
        "vlr_dolar": costs["exchange_rate"],
        "cost_usd": costs["cost_usd"],
        "cost_brl": costs["cost_brl"],
        "price_source": costs["price_source"],
        "cost_breakdown": costs["cost_breakdown"],
        "timestamp": datetime.now().isoformat()
    }

    if prompt_id:
        enhanced_metadata["prompt_id"] = prompt_id

    # 6. ENVIAR PARA ADMIN CENTER
    track_success = counter.admin_center.track_token_usage(
        model_name=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        endpoint_called=endpoint,
        user_id=user_id,
        prompt_id=prompt_id,
        metadata=enhanced_metadata
    )

    # 7. LOG DE USO DO PROMPT
    if prompt_id and track_success:
        counter.admin_center.log_prompt_usage(
            prompt_id=prompt_id,
            variables_used={},
            final_prompt=prompt_text[:2000] if prompt_text else None,
            tokens_used=prompt_tokens + completion_tokens,
            model_used=model
        )

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "source": source,
        "provider": provider,
        "model": model,
        "prompt_id": prompt_id,
        "admin_center_tracked": track_success,
        "timestamp": datetime.now().isoformat(),
        **costs
    }


# ============================================
# FUNCOES DE DETECCAO E EXTRACAO
# ============================================

def _detect_provider(response: Any) -> str:
    """Detecta automaticamente o provider baseado na estrutura do objeto"""
    response_type = type(response).__name__
    module = type(response).__module__

    if "openai" in module.lower():
        return "openai"
    if "langchain" in module.lower():
        return "langchain"
    if "anthropic" in module.lower():
        return "anthropic"
    if "google" in module.lower() or "generativeai" in module.lower():
        return "google"

    # Deteccao por estrutura
    if hasattr(response, "usage"):
        if hasattr(response.usage, "prompt_tokens"):
            return "openai"
        elif hasattr(response.usage, "input_tokens"):
            return "anthropic"

    if hasattr(response, "llm_output"):
        return "langchain"

    if hasattr(response, "candidates"):
        return "google"

    return "unknown"


def _extract_openai_tokens(response: Any) -> Optional[Dict[str, int]]:
    try:
        if hasattr(response, "usage"):
            usage = response.usage
            return {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens
            }
    except Exception as e:
        logging.debug(f"Erro OpenAI tokens: {e}")
    return None


def _extract_langchain_tokens(response: Any) -> Optional[Dict[str, int]]:
    try:
        if hasattr(response, "llm_output") and isinstance(response.llm_output, dict):
            usage = response.llm_output.get("token_usage", {})
            if usage:
                return {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0)
                }
        if hasattr(response, "usage"):
            return _extract_openai_tokens(response)
    except Exception as e:
        logging.debug(f"Erro LangChain tokens: {e}")
    return None


def _extract_anthropic_tokens(response: Any) -> Optional[Dict[str, int]]:
    try:
        if hasattr(response, "usage"):
            usage = response.usage
            if hasattr(usage, "input_tokens"):
                return {
                    "prompt_tokens": usage.input_tokens,
                    "completion_tokens": usage.output_tokens,
                    "total_tokens": usage.input_tokens + usage.output_tokens
                }
    except Exception as e:
        logging.debug(f"Erro Anthropic tokens: {e}")
    return None


def _extract_google_tokens(response: Any) -> Optional[Dict[str, int]]:
    """Extrai tokens de resposta Google Gemini"""
    try:
        if hasattr(response, "usage_metadata"):
            meta = response.usage_metadata
            prompt_tokens = getattr(meta, "prompt_token_count", 0)
            completion_tokens = getattr(meta, "candidates_token_count", 0)
            total = getattr(meta, "total_token_count", prompt_tokens + completion_tokens)
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total
            }
    except Exception as e:
        logging.debug(f"Erro Google tokens: {e}")
    return None


def _extract_response_text(response: Any, provider: str) -> str:
    try:
        if provider == "openai":
            if hasattr(response, "choices") and response.choices:
                return response.choices[0].message.content or ""
        elif provider == "langchain":
            if hasattr(response, "text"):
                return response.text
            if hasattr(response, "content"):
                return response.content
            if hasattr(response, "generations"):
                return response.generations[0][0].text
        elif provider == "anthropic":
            if hasattr(response, "content"):
                if isinstance(response.content, list):
                    return " ".join([c.text for c in response.content if hasattr(c, "text")])
                return response.content
        elif provider == "google":
            if hasattr(response, "text"):
                return response.text
            if hasattr(response, "candidates") and response.candidates:
                parts = response.candidates[0].content.parts
                return " ".join([p.text for p in parts if hasattr(p, "text")])

        if hasattr(response, "content"):
            return str(response.content)
        if hasattr(response, "text"):
            return str(response.text)
    except Exception as e:
        logging.debug(f"Erro ao extrair texto: {e}")
    return ""


# ============================================
# FUNCOES DE COMPATIBILIDADE
# ============================================

def track_openai_call(
    prompt: str,
    response: str,
    model: str = "gpt-3.5-turbo",
    endpoint: str = "/openai_direct",
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """DEPRECATED: Use track_api_response() com o objeto completo"""
    logging.warning("track_openai_call() deprecated. Use track_api_response().")

    counter = HybridTokenCounter(model)
    prompt_tokens = count_tokens_tiktoken(prompt, model)
    completion_tokens = count_tokens_tiktoken(response, model)
    costs = counter.calculate_costs(prompt_tokens, completion_tokens)

    track_success = counter.admin_center.track_token_usage(
        model_name=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        endpoint_called=endpoint,
        user_id=user_id,
        metadata={"token_source": "tiktoken_legacy", **costs}
    )

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "source": "tiktoken_legacy",
        "model": model,
        "admin_center_tracked": track_success,
        **costs
    }


def estimate_tokens_and_cost(
    prompt: str,
    model: str = "gpt-3.5-turbo",
    estimated_response_length: int = 100
) -> Dict[str, Any]:
    """Estimativa previa (antes de chamar a API)"""
    counter = HybridTokenCounter(model)

    # Usar contagem inteligente
    smart = count_tokens_smart(prompt, model)
    prompt_tokens = smart["count"]
    completion_tokens = estimated_response_length

    costs = counter.calculate_costs(prompt_tokens, completion_tokens)

    return {
        "prompt_tokens": prompt_tokens,
        "estimated_completion_tokens": completion_tokens,
        "estimated_total_tokens": prompt_tokens + completion_tokens,
        "source": f"estimation_{smart['source']}",
        "model": model,
        **costs
    }


# ============================================
# LANGCHAIN CALLBACK
# ============================================

if LANGCHAIN_AVAILABLE:
    class LangChainTokenCallback(BaseCallbackHandler):
        def __init__(self, model: str = "gpt-3.5-turbo", endpoint: str = "/langchain"):
            super().__init__()
            self.model = model
            self.endpoint = endpoint
            self.admin_center = get_admin_center_service()
            self.counter = HybridTokenCounter(model)

        def on_llm_end(self, response: Any, **kwargs) -> None:
            try:
                tokens = extract_tokens_from_response(response)
                if not tokens:
                    return

                costs = self.counter.calculate_costs(tokens["prompt_tokens"], tokens["completion_tokens"])

                self.admin_center.track_token_usage(
                    model_name=self.model,
                    prompt_tokens=tokens["prompt_tokens"],
                    completion_tokens=tokens["completion_tokens"],
                    endpoint_called=self.endpoint,
                    metadata={"langchain_callback": True, **costs}
                )
            except Exception as e:
                logging.error(f"Erro no callback LangChain: {e}")
else:
    class LangChainTokenCallback:
        def __init__(self, model: str = "gpt-3.5-turbo", endpoint: str = "/langchain"):
            logging.warning("LangChain nao disponivel.")

        def on_llm_end(self, response: Any, **kwargs) -> None:
            pass


# Alias para compatibilidade
count_tokens = count_tokens_tiktoken

def invalidate_model_price_cache(model_name: str = None):
    """Invalida cache de precos"""
    logging.info(f"Cache de precos invalidado: {model_name or 'todos'}")
