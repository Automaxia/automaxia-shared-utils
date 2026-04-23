"""
Modulo hibrido para contagem de tokens - Arquitetura multi-nivel
v1.3.0 - Prompt caching + reasoning tokens + correcoes de cache/thread-safety

Hierarquia de precisao:
  1. response.usage (da API) -> 100% exato
  2. LiteLLM token_counter -> universal, 100+ modelos
  3. APIs nativas (Anthropic/Google) -> exato por provider
  4. tiktoken -> fallback offline (preciso para OpenAI)
  5. len(text) // 4 -> ultimo recurso

Suporta:
  - Prompt caching (Anthropic cache_read/cache_creation, OpenAI cached_tokens)
  - Reasoning tokens (OpenAI o1, Claude extended thinking)
  - Mensagens estruturadas [{role, content}] alem de str
  - Client Anthropic singleton (evita recriar a cada call)
  - Cache de precos compartilhado entre instancias
  - CurrencyService thread-safe com override via USD_BRL_RATE
"""
import os
import logging
import requests
import threading
from typing import Dict, Any, List, Optional, Union, Tuple
from datetime import datetime, timedelta
import tiktoken

# ============================================
# IMPORTS OPCIONAIS
# ============================================

try:
    import litellm
    from litellm import token_counter as litellm_token_counter
    from litellm import cost_per_token as litellm_cost_per_token
    LITELLM_AVAILABLE = True
except ImportError:
    LITELLM_AVAILABLE = False
    logging.warning("LiteLLM nao disponivel. Usando tiktoken como fallback.")

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    import google.generativeai as genai
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

try:
    from langchain.callbacks.base import BaseCallbackHandler
    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False

from automaxia_utils.admin_center.service import get_admin_center_service


# ============================================
# SERVICO DE COTACAO (thread-safe)
# ============================================

class CurrencyService:
    """Servico thread-safe para obter cotacao USD/BRL.

    Ordem de resolucao:
      1. Env var USD_BRL_RATE (fixa, ignora API)
      2. Cache em memoria (TTL 30 min)
      3. API exchangerate-api.com
      4. Cache stale (ultimo valor bom)
      5. Env var USD_BRL_FALLBACK ou 5.0
    """

    def __init__(self):
        self._cached_rate: Optional[float] = None
        self._cache_timestamp: Optional[datetime] = None
        self._cache_duration = timedelta(minutes=30)
        self._lock = threading.Lock()

    def _override_rate(self) -> Optional[float]:
        raw = os.getenv("USD_BRL_RATE")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            logging.warning(f"USD_BRL_RATE invalido: {raw!r}")
            return None

    def _fallback_rate(self) -> float:
        try:
            return float(os.getenv("USD_BRL_FALLBACK", "5.0"))
        except ValueError:
            return 5.0

    def get_usd_to_brl_rate(self) -> float:
        override = self._override_rate()
        if override is not None:
            return override

        with self._lock:
            now = datetime.now()
            if (self._cached_rate is not None and self._cache_timestamp and
                    now - self._cache_timestamp < self._cache_duration):
                return self._cached_rate

            try:
                response = requests.get(
                    "https://api.exchangerate-api.com/v4/latest/USD",
                    timeout=5
                )
                response.raise_for_status()
                rate = float(response.json()['rates']['BRL'])
                self._cached_rate = rate
                self._cache_timestamp = now
                return rate
            except Exception as e:
                logging.warning(f"Erro ao obter cotacao USD/BRL: {e}")
                if self._cached_rate is not None:
                    return self._cached_rate
                return self._fallback_rate()


currency_service = CurrencyService()


# ============================================
# SINGLETONS DE CLIENT
# ============================================

_anthropic_client: Optional[Any] = None
_anthropic_client_lock = threading.Lock()


def _get_anthropic_client():
    """Retorna client Anthropic singleton (lazy)."""
    global _anthropic_client
    if not ANTHROPIC_AVAILABLE:
        return None
    if _anthropic_client is None:
        with _anthropic_client_lock:
            if _anthropic_client is None:
                try:
                    _anthropic_client = anthropic.Anthropic()
                except Exception as e:
                    logging.debug(f"Falha ao criar client Anthropic: {e}")
                    return None
    return _anthropic_client


# ============================================
# HELPERS DE NORMALIZACAO
# ============================================

def _get_attr(obj: Any, name: str, default: Any = 0) -> Any:
    """Leitura unificada de atributo (objeto) ou chave (dict), com default."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _normalize_to_messages(text_or_messages: Union[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Converte str em formato messages=[{role, content}]."""
    if isinstance(text_or_messages, str):
        return [{"role": "user", "content": text_or_messages}]
    return text_or_messages


def _extract_text(text_or_messages: Union[str, List[Dict[str, Any]]]) -> str:
    """Extrai texto cru para contadores que nao aceitam messages estruturadas."""
    if isinstance(text_or_messages, str):
        return text_or_messages
    parts = []
    for m in text_or_messages or []:
        content = m.get("content", "") if isinstance(m, dict) else ""
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict):
                    parts.append(c.get("text", ""))
                else:
                    parts.append(str(c))
        else:
            parts.append(str(content))
    return "\n".join(parts)


# ============================================
# CONTAGEM DE TOKENS - MULTI-NIVEL
# ============================================

def count_tokens_litellm(
    text_or_messages: Union[str, List[Dict[str, Any]]], model: str
) -> Optional[int]:
    """Nivel 2: Contagem via LiteLLM (universal, 100+ modelos)."""
    if not LITELLM_AVAILABLE:
        return None
    try:
        messages = _normalize_to_messages(text_or_messages)
        count = litellm_token_counter(model=model, messages=messages)
        logging.debug(f"LiteLLM token count para '{model}': {count}")
        return count
    except Exception as e:
        logging.debug(f"LiteLLM token count falhou para '{model}': {e}")
        return None


def count_tokens_anthropic_native(
    text_or_messages: Union[str, List[Dict[str, Any]]], model: str
) -> Optional[int]:
    """Nivel 3: Contagem nativa Anthropic (exato para Claude).

    Usa o novo endpoint client.messages.count_tokens quando disponivel,
    com fallback para o legado client.count_tokens (deprecated).
    """
    client = _get_anthropic_client()
    if not client or "claude" not in model.lower():
        return None

    messages = _normalize_to_messages(text_or_messages)

    # SDK novo (>=0.21): client.messages.count_tokens
    try:
        messages_api = getattr(client, "messages", None)
        if messages_api and hasattr(messages_api, "count_tokens"):
            result = messages_api.count_tokens(model=model, messages=messages)
            count = _get_attr(result, "input_tokens", None)
            if count is not None:
                logging.debug(f"Anthropic messages.count_tokens para '{model}': {count}")
                return count
    except Exception as e:
        logging.debug(f"Anthropic messages.count_tokens falhou: {e}")

    # Fallback: SDK antigo (deprecated mas pode estar em uso)
    try:
        if hasattr(client, "count_tokens"):
            text = _extract_text(text_or_messages)
            count = client.count_tokens(text)
            logging.debug(f"Anthropic legacy count_tokens para '{model}': {count}")
            return count
    except Exception as e:
        logging.debug(f"Anthropic legacy count_tokens falhou: {e}")

    return None


def count_tokens_google_native(
    text_or_messages: Union[str, List[Dict[str, Any]]], model: str
) -> Optional[int]:
    """Nivel 3: Contagem nativa Google (exato para Gemini)."""
    if not GOOGLE_AVAILABLE or "gemini" not in model.lower():
        return None
    try:
        text = _extract_text(text_or_messages)
        gmodel = genai.GenerativeModel(model)
        result = gmodel.count_tokens(text)
        count = result.total_tokens
        logging.debug(f"Google native count para '{model}': {count}")
        return count
    except Exception as e:
        logging.debug(f"Google native count falhou: {e}")
        return None


def count_tokens_tiktoken(
    text_or_messages: Union[str, List[Dict[str, Any]]], model: str = "gpt-3.5-turbo"
) -> int:
    """Nivel 4: Contagem via tiktoken (fallback offline, preciso para OpenAI)."""
    text = text_or_messages if isinstance(text_or_messages, str) else _extract_text(text_or_messages)
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


def count_tokens_smart(
    text_or_messages: Union[str, List[Dict[str, Any]]], model: str
) -> Dict[str, Any]:
    """Contagem inteligente multi-nivel. Aceita str ou messages=[{role, content}].

    Para Claude/Gemini, prefere contagem nativa sobre LiteLLM (mais atualizada).
    """
    model_lower = model.lower()

    if "claude" in model_lower:
        count = count_tokens_anthropic_native(text_or_messages, model)
        if count is not None:
            return {"count": count, "source": "anthropic_native"}

    if "gemini" in model_lower:
        count = count_tokens_google_native(text_or_messages, model)
        if count is not None:
            return {"count": count, "source": "google_native"}

    count = count_tokens_litellm(text_or_messages, model)
    if count is not None:
        return {"count": count, "source": "litellm"}

    count = count_tokens_tiktoken(text_or_messages, model)
    return {"count": count, "source": "tiktoken"}


# ============================================
# EXTRACAO DE TOKENS DA RESPOSTA
# ============================================

def _normalize_usage(usage: Any) -> Optional[Dict[str, int]]:
    """Normaliza usage (dict ou objeto) para formato padronizado.

    Inclui cache_read_tokens, cache_creation_tokens e reasoning_tokens.
    Convencao: prompt_tokens = total de tokens de entrada (incluindo cache).
    """
    if usage is None:
        return None

    # Formato OpenAI (prompt_tokens como total, inclui cached)
    prompt_tokens = _get_attr(usage, 'prompt_tokens', None)
    if prompt_tokens is not None:
        completion_tokens = _get_attr(usage, 'completion_tokens', 0) or 0
        total_tokens = _get_attr(usage, 'total_tokens', prompt_tokens + completion_tokens)

        cached_tokens = 0
        prompt_details = _get_attr(usage, 'prompt_tokens_details', None)
        if prompt_details is not None:
            cached_tokens = _get_attr(prompt_details, 'cached_tokens', 0) or 0

        reasoning_tokens = 0
        completion_details = _get_attr(usage, 'completion_tokens_details', None)
        if completion_details is not None:
            reasoning_tokens = _get_attr(completion_details, 'reasoning_tokens', 0) or 0

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cache_read_tokens": cached_tokens,
            "cache_creation_tokens": 0,
            "reasoning_tokens": reasoning_tokens,
        }

    # Formato Anthropic (input_tokens NAO inclui cached - campos separados)
    input_tokens = _get_attr(usage, 'input_tokens', None)
    if input_tokens is not None:
        output_tokens = _get_attr(usage, 'output_tokens', 0) or 0
        cache_read = _get_attr(usage, 'cache_read_input_tokens', 0) or 0
        cache_creation = _get_attr(usage, 'cache_creation_input_tokens', 0) or 0

        # Normalizar para convencao OpenAI (prompt_tokens = total de entrada)
        total_prompt = input_tokens + cache_read + cache_creation

        return {
            "prompt_tokens": total_prompt,
            "completion_tokens": output_tokens,
            "total_tokens": total_prompt + output_tokens,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
            "reasoning_tokens": 0,
        }

    return None


def extract_tokens_from_response(response: Any) -> Optional[Dict[str, int]]:
    """Nivel 1: Extrai tokens REAIS da resposta da API (mais preciso possivel).

    Retorna dict com prompt/completion/total + cache_read/cache_creation + reasoning.
    """
    try:
        usage = None
        if hasattr(response, 'usage'):
            usage = response.usage
        elif isinstance(response, dict) and 'usage' in response:
            usage = response['usage']
        elif hasattr(response, 'llm_output') and isinstance(response.llm_output, dict):
            usage = response.llm_output.get('token_usage')

        return _normalize_usage(usage)
    except Exception as e:
        logging.error(f"Erro ao extrair tokens: {e}")
        return None


# ============================================
# CACHE DE PRECOS COMPARTILHADO
# ============================================

_price_cache: Dict[str, Dict[str, Any]] = {}
_price_cache_lock = threading.Lock()
_PRICE_CACHE_TTL = timedelta(hours=1)


def invalidate_model_price_cache(model_name: str = None):
    """Invalida cache de precos de modelos (compartilhado entre instancias)."""
    with _price_cache_lock:
        if model_name:
            _price_cache.pop(model_name, None)
            logging.info(f"Cache de precos invalidado para: {model_name}")
        else:
            _price_cache.clear()
            logging.info("Cache de precos invalidado: todos os modelos")


# ============================================
# CALCULO DE CUSTOS
# ============================================

class HybridTokenCounter:
    """Contador hibrido com custos via LiteLLM + API AdminCenter + fallback.

    Suporta pricing diferenciado para cache read (desconto) e cache creation (premium).
    """

    # Precos fallback (Mar 2026) por 1K tokens em USD
    # cache_read_discount: multiplicador do input cost para cache reads (Anthropic 0.1, OpenAI 0.5)
    # cache_write_multiplier: multiplicador do input cost para cache creation (Anthropic 1.25, OpenAI 1.0)
    FALLBACK_PRICES_USD = {
        "gpt-4o": {"input": 0.0025, "output": 0.01, "cache_read_discount": 0.5},
        "gpt-4o-mini": {"input": 0.00015, "output": 0.0006, "cache_read_discount": 0.5},
        "gpt-4-turbo": {"input": 0.01, "output": 0.03},
        "gpt-4": {"input": 0.03, "output": 0.06},
        "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
        "o1": {"input": 0.015, "output": 0.06, "cache_read_discount": 0.5},
        "o1-mini": {"input": 0.003, "output": 0.012, "cache_read_discount": 0.5},
        "claude-opus-4": {"input": 0.015, "output": 0.075,
                          "cache_read_discount": 0.1, "cache_write_multiplier": 1.25},
        "claude-sonnet-4": {"input": 0.003, "output": 0.015,
                            "cache_read_discount": 0.1, "cache_write_multiplier": 1.25},
        "claude-haiku-4": {"input": 0.0008, "output": 0.004,
                           "cache_read_discount": 0.1, "cache_write_multiplier": 1.25},
        "claude-sonnet-4-20250514": {"input": 0.003, "output": 0.015,
                                     "cache_read_discount": 0.1, "cache_write_multiplier": 1.25},
        "claude-3-5-sonnet-20241022": {"input": 0.003, "output": 0.015,
                                       "cache_read_discount": 0.1, "cache_write_multiplier": 1.25},
        "claude-3-5-haiku-20241022": {"input": 0.0008, "output": 0.004,
                                      "cache_read_discount": 0.1, "cache_write_multiplier": 1.25},
        "claude-3-opus-20240229": {"input": 0.015, "output": 0.075,
                                   "cache_read_discount": 0.1, "cache_write_multiplier": 1.25},
        "gemini-1.5-pro": {"input": 0.00125, "output": 0.005},
        "gemini-1.5-flash": {"input": 0.000075, "output": 0.0003},
        "gemini-2.0-flash": {"input": 0.0001, "output": 0.0004},
    }

    # Prefixos ordenados do mais especifico ao menos (usado em fallback match)
    _MODEL_PREFIX_MAP = [
        ("claude-opus-4", "claude-opus-4"),
        ("claude-sonnet-4", "claude-sonnet-4"),
        ("claude-haiku-4", "claude-haiku-4"),
        ("claude-3-5-sonnet", "claude-3-5-sonnet-20241022"),
        ("claude-3-5-haiku", "claude-3-5-haiku-20241022"),
        ("claude-3-opus", "claude-3-opus-20240229"),
        ("claude-opus", "claude-opus-4"),
        ("claude-sonnet", "claude-sonnet-4"),
        ("claude-haiku", "claude-haiku-4"),
        ("claude", "claude-sonnet-4"),
        ("o1-mini", "o1-mini"),
        ("o1", "o1"),
        ("gpt-4o-mini", "gpt-4o-mini"),
        ("gpt-4o", "gpt-4o"),
        ("gpt-4-turbo", "gpt-4-turbo"),
        ("gpt-4", "gpt-4"),
        ("gpt-3.5", "gpt-3.5-turbo"),
        ("gemini-2.0-flash", "gemini-2.0-flash"),
        ("gemini-1.5-pro", "gemini-1.5-pro"),
        ("gemini-1.5-flash", "gemini-1.5-flash"),
        ("gemini", "gemini-1.5-flash"),
    ]

    def __init__(self, model: str = "gpt-3.5-turbo"):
        self.model = model
        self.admin_center = get_admin_center_service()

    def calculate_costs(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> Dict[str, Any]:
        """Calcula custos em USD e BRL considerando cache tokens.

        reasoning_tokens nao tem pricing separado - ja incluso em completion_tokens
        pelas APIs (OpenAI inclui reasoning em completion, Anthropic em output).
        """
        costs = self._calculate_via_litellm(
            prompt_tokens, completion_tokens, cache_read_tokens, cache_creation_tokens
        )
        if costs:
            return costs

        costs = self._calculate_via_api(
            prompt_tokens, completion_tokens, cache_read_tokens, cache_creation_tokens
        )
        if costs:
            return costs

        return self._calculate_via_fallback(
            prompt_tokens, completion_tokens, cache_read_tokens, cache_creation_tokens
        )

    def _calculate_via_litellm(
        self, prompt_tokens: int, completion_tokens: int,
        cache_read_tokens: int, cache_creation_tokens: int
    ) -> Optional[Dict]:
        if not LITELLM_AVAILABLE:
            return None
        try:
            # Regular input = total menos tokens de cache (evita cobranca dupla)
            regular_input = max(0, prompt_tokens - cache_read_tokens - cache_creation_tokens)
            prompt_cost, completion_cost = litellm_cost_per_token(
                model=self.model,
                prompt_tokens=regular_input,
                completion_tokens=completion_tokens,
            )

            cache_read_cost, cache_creation_cost = self._litellm_cache_cost(
                cache_read_tokens, cache_creation_tokens
            )

            total_usd = prompt_cost + completion_cost + cache_read_cost + cache_creation_cost
            return self._format_costs(
                prompt_cost, completion_cost, cache_read_cost, cache_creation_cost,
                total_usd, "litellm"
            )
        except Exception as e:
            logging.debug(f"LiteLLM cost falhou para '{self.model}': {e}")
            return None

    def _litellm_cache_cost(
        self, cache_read_tokens: int, cache_creation_tokens: int
    ) -> Tuple[float, float]:
        """Obtem custo de cache via model_cost do LiteLLM, com fallback heuristico."""
        if not (cache_read_tokens or cache_creation_tokens):
            return 0.0, 0.0

        model_info: Dict[str, Any] = {}
        try:
            model_info = litellm.model_cost.get(self.model) or {}
        except Exception:
            model_info = {}

        input_rate = model_info.get("input_cost_per_token", 0) or 0
        cache_read_rate = model_info.get("cache_read_input_token_cost")
        cache_creation_rate = model_info.get("cache_creation_input_token_cost")

        # Heuristica por provider quando LiteLLM nao tem os campos
        model_lower = self.model.lower()
        if cache_read_rate is None:
            if "claude" in model_lower:
                cache_read_rate = input_rate * 0.1
            elif any(p in model_lower for p in ["gpt", "o1"]):
                cache_read_rate = input_rate * 0.5
            else:
                cache_read_rate = input_rate

        if cache_creation_rate is None:
            if "claude" in model_lower:
                cache_creation_rate = input_rate * 1.25
            else:
                cache_creation_rate = 0  # OpenAI nao cobra cache write

        return (
            cache_read_tokens * cache_read_rate,
            cache_creation_tokens * cache_creation_rate,
        )

    def _calculate_via_api(
        self, prompt_tokens: int, completion_tokens: int,
        cache_read_tokens: int, cache_creation_tokens: int
    ) -> Optional[Dict]:
        try:
            if not self.admin_center.config.enabled:
                return None

            prices = self._get_api_prices()
            if prices is None:
                return None

            return self._build_cost_result(
                prompt_tokens, completion_tokens, cache_read_tokens, cache_creation_tokens,
                prices, "admin_center_api"
            )
        except Exception as e:
            logging.debug(f"API cost falhou para '{self.model}': {e}")
            return None

    def _get_api_prices(self) -> Optional[Dict[str, float]]:
        """Busca precos da API AdminCenter com cache compartilhado (1h TTL)."""
        now = datetime.now()

        with _price_cache_lock:
            cached = _price_cache.get(self.model)
            if cached and now - cached["timestamp"] < _PRICE_CACHE_TTL:
                return cached["prices"]

        try:
            params = {"name": self.model}
            response = self.admin_center._make_request(
                "GET", "/ai-model/consulta_nome", params=params
            )

            if not response or "data" not in response:
                return None

            data = response["data"]
            input_cost = data.get("input_cost_per_token")
            output_cost = data.get("output_cost_per_token")

            if input_cost is None or output_cost is None:
                return None

            # API retorna custo por 1 token; convertemos para 1K
            prices: Dict[str, float] = {
                "input": float(input_cost) * 1000,
                "output": float(output_cost) * 1000,
            }

            cache_read = data.get("cache_read_input_cost_per_token")
            cache_creation = data.get("cache_creation_input_cost_per_token")
            if cache_read is not None:
                prices["cache_read"] = float(cache_read) * 1000
            if cache_creation is not None:
                prices["cache_creation"] = float(cache_creation) * 1000

            with _price_cache_lock:
                _price_cache[self.model] = {"prices": prices, "timestamp": now}

            return prices
        except Exception as e:
            logging.debug(f"API price fetch falhou para '{self.model}': {e}")
            return None

    def _calculate_via_fallback(
        self, prompt_tokens: int, completion_tokens: int,
        cache_read_tokens: int, cache_creation_tokens: int
    ) -> Dict:
        """Fallback hardcoded com match por prefixo (evita default sempre para gpt-3.5)."""
        fallback_key = self._match_fallback_model()
        prices = self.FALLBACK_PRICES_USD.get(fallback_key, {"input": 0.001, "output": 0.002})
        return self._build_cost_result(
            prompt_tokens, completion_tokens, cache_read_tokens, cache_creation_tokens,
            prices, f"fallback_hardcoded({fallback_key})"
        )

    def _match_fallback_model(self) -> str:
        """Match por prefixo ordenado - evita subestimar custo de modelos caros."""
        if self.model in self.FALLBACK_PRICES_USD:
            return self.model

        model_lower = self.model.lower()
        for prefix, key in self._MODEL_PREFIX_MAP:
            if prefix in model_lower:
                return key

        logging.warning(
            f"Modelo '{self.model}' desconhecido, usando gpt-4o-mini como fallback"
        )
        return "gpt-4o-mini"

    def _build_cost_result(
        self, prompt_tokens: int, completion_tokens: int,
        cache_read_tokens: int, cache_creation_tokens: int,
        prices: Dict[str, float], source: str
    ) -> Dict:
        regular_input = max(0, prompt_tokens - cache_read_tokens - cache_creation_tokens)

        cost_input_usd = (regular_input / 1000) * prices["input"]
        cost_output_usd = (completion_tokens / 1000) * prices["output"]

        # Rates para cache (com heuristica por provider como default)
        model_lower = self.model.lower()
        default_read_discount = 0.1 if "claude" in model_lower else 0.5
        default_write_multiplier = 1.25 if "claude" in model_lower else 1.0

        read_discount = prices.get("cache_read_discount", default_read_discount)
        write_multiplier = prices.get("cache_write_multiplier", default_write_multiplier)

        cache_read_rate = prices.get("cache_read", prices["input"] * read_discount)
        cache_creation_rate = prices.get("cache_creation", prices["input"] * write_multiplier)

        cost_cache_read_usd = (cache_read_tokens / 1000) * cache_read_rate
        cost_cache_creation_usd = (cache_creation_tokens / 1000) * cache_creation_rate

        total_cost_usd = (
            cost_input_usd + cost_output_usd + cost_cache_read_usd + cost_cache_creation_usd
        )

        return self._format_costs(
            cost_input_usd, cost_output_usd, cost_cache_read_usd, cost_cache_creation_usd,
            total_cost_usd, source
        )

    def _format_costs(
        self, input_usd: float, output_usd: float,
        cache_read_usd: float, cache_creation_usd: float,
        total_usd: float, source: str
    ) -> Dict:
        exchange_rate = currency_service.get_usd_to_brl_rate()
        return {
            "cost_usd": round(total_usd, 6),
            "cost_brl": round(total_usd * exchange_rate, 4),
            "exchange_rate": exchange_rate,
            "price_source": source,
            "cost_breakdown": {
                "input_usd": round(input_usd, 6),
                "output_usd": round(output_usd, 6),
                "cache_read_usd": round(cache_read_usd, 6),
                "cache_creation_usd": round(cache_creation_usd, 6),
                "input_brl": round(input_usd * exchange_rate, 4),
                "output_brl": round(output_usd * exchange_rate, 4),
                "cache_read_brl": round(cache_read_usd * exchange_rate, 4),
                "cache_creation_brl": round(cache_creation_usd * exchange_rate, 4),
            }
        }


# ============================================
# DETECCAO E EXTRACAO DE TEXTO
# ============================================

def _detect_provider(response: Any) -> str:
    """Detecta provider por modulo + estrutura."""
    try:
        module = type(response).__module__.lower()

        if "openai" in module:
            return "openai"
        if "langchain" in module:
            return "langchain"
        if "anthropic" in module:
            return "anthropic"
        if "google" in module or "generativeai" in module:
            return "google"

        # Deteccao por estrutura
        if hasattr(response, "usage"):
            usage = response.usage
            if hasattr(usage, "input_tokens"):
                return "anthropic"
            if hasattr(usage, "prompt_tokens"):
                return "openai"

        if hasattr(response, "llm_output"):
            return "langchain"

        if hasattr(response, "candidates") or hasattr(response, "usage_metadata"):
            return "google"
    except Exception as e:
        logging.debug(f"Erro ao detectar provider: {e}")

    return "unknown"


def _extract_response_text(response: Any, provider: str) -> str:
    """Extrai o texto de uma resposta para contar tokens em fallback."""
    try:
        if provider == "openai":
            choices = getattr(response, "choices", None)
            if choices:
                msg = getattr(choices[0], "message", None)
                if msg:
                    return getattr(msg, "content", "") or ""
        elif provider == "langchain":
            if hasattr(response, "text"):
                return response.text or ""
            if hasattr(response, "content"):
                return response.content or ""
            generations = getattr(response, "generations", None)
            if generations and len(generations) > 0 and len(generations[0]) > 0:
                return getattr(generations[0][0], "text", "") or ""
        elif provider == "anthropic":
            content = getattr(response, "content", None)
            if content:
                if isinstance(content, list):
                    return " ".join(
                        getattr(c, "text", "") for c in content if hasattr(c, "text")
                    )
                return str(content)
        elif provider == "google":
            if hasattr(response, "text"):
                return response.text or ""
            candidates = getattr(response, "candidates", None)
            if candidates:
                content = getattr(candidates[0], "content", None)
                parts = getattr(content, "parts", []) if content else []
                return " ".join(
                    getattr(p, "text", "") for p in parts if hasattr(p, "text")
                )

        # Fallback generico
        if hasattr(response, "content"):
            return str(response.content or "")
        if hasattr(response, "text"):
            return str(response.text or "")
    except Exception as e:
        logging.debug(f"Erro ao extrair texto: {e}")
    return ""


# ============================================
# FUNCAO PRINCIPAL (RECOMENDADA)
# ============================================

def track_api_response(
    response: Any,
    model: str,
    endpoint: str = "/api_direct",
    user_id: Optional[str] = None,
    prompt_text: Union[str, List[Dict[str, Any]]] = "",
    prompt_id: Optional[str] = None,
    force_provider: Optional[str] = None
) -> Dict[str, Any]:
    """Funcao universal para tracking de tokens + custos.

    Detecta automaticamente provider (OpenAI, LangChain, Anthropic, Google).
    Suporta prompt caching (Anthropic/OpenAI) e reasoning tokens (o1).

    Returns:
        Dict com tokens, costs, cache_read_tokens, cache_creation_tokens,
        reasoning_tokens, e metadata.
    """
    counter = HybridTokenCounter(model)
    provider = force_provider or _detect_provider(response)

    # Nivel 1: extrair tokens reais do response
    tokens = extract_tokens_from_response(response)
    source = f"{provider}_api" if tokens else None

    # Fallback multi-nivel se response.usage nao disponivel
    if not tokens:
        smart = count_tokens_smart(prompt_text, model)
        prompt_tokens = smart["count"]
        source = smart["source"] + "_fallback"

        response_text = _extract_response_text(response, provider)
        completion_tokens = 0
        if response_text:
            completion_tokens = count_tokens_smart(response_text, model)["count"]

        tokens = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "reasoning_tokens": 0,
        }

    prompt_tokens = tokens["prompt_tokens"]
    completion_tokens = tokens["completion_tokens"]
    cache_read = tokens.get("cache_read_tokens", 0)
    cache_creation = tokens.get("cache_creation_tokens", 0)
    reasoning = tokens.get("reasoning_tokens", 0)

    logging.info(
        f"Tokens via {source} (provider: {provider}): "
        f"prompt={prompt_tokens}, completion={completion_tokens}, "
        f"cache_read={cache_read}, cache_creation={cache_creation}, reasoning={reasoning}"
    )

    costs = counter.calculate_costs(
        prompt_tokens, completion_tokens, cache_read, cache_creation, reasoning
    )

    # Preview do prompt (aceita str ou messages)
    prompt_preview = (
        prompt_text[:500] if isinstance(prompt_text, str) else str(prompt_text)[:500]
    )

    enhanced_metadata = {
        "prompt_text": prompt_preview,
        "model_name": model,
        "provider": provider,
        "token_source": source,
        "vlr_dolar": costs["exchange_rate"],
        "cost_usd": costs["cost_usd"],
        "cost_brl": costs["cost_brl"],
        "price_source": costs["price_source"],
        "cost_breakdown": costs["cost_breakdown"],
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "reasoning_tokens": reasoning,
        "timestamp": datetime.now().isoformat(),
    }

    if prompt_id:
        enhanced_metadata["prompt_id"] = prompt_id

    track_success = counter.admin_center.track_token_usage(
        model_name=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        endpoint_called=endpoint,
        user_id=user_id,
        prompt_id=prompt_id,
        metadata=enhanced_metadata,
    )

    if prompt_id and track_success:
        prompt_log = (
            prompt_text[:2000] if isinstance(prompt_text, str) else str(prompt_text)[:2000]
        )
        counter.admin_center.log_prompt_usage(
            prompt_id=prompt_id,
            variables_used={},
            final_prompt=prompt_log,
            tokens_used=prompt_tokens + completion_tokens,
            model_used=model,
        )

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "reasoning_tokens": reasoning,
        "source": source,
        "provider": provider,
        "model": model,
        "prompt_id": prompt_id,
        "admin_center_tracked": track_success,
        "timestamp": datetime.now().isoformat(),
        **costs,
    }


# ============================================
# FUNCOES DE COMPATIBILIDADE
# ============================================

def track_openai_call(
    prompt: str,
    response: str,
    model: str = "gpt-3.5-turbo",
    endpoint: str = "/openai_direct",
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """DEPRECATED: Use track_api_response() com o objeto completo."""
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
        metadata={"token_source": "tiktoken_legacy", **costs},
    )

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "source": "tiktoken_legacy",
        "model": model,
        "admin_center_tracked": track_success,
        **costs,
    }


def estimate_tokens_and_cost(
    prompt: Union[str, List[Dict[str, Any]]],
    model: str = "gpt-3.5-turbo",
    estimated_response_length: int = 100,
) -> Dict[str, Any]:
    """Estimativa previa (antes de chamar a API). Aceita str ou messages."""
    counter = HybridTokenCounter(model)

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
        **costs,
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

                costs = self.counter.calculate_costs(
                    tokens["prompt_tokens"],
                    tokens["completion_tokens"],
                    tokens.get("cache_read_tokens", 0),
                    tokens.get("cache_creation_tokens", 0),
                )

                self.admin_center.track_token_usage(
                    model_name=self.model,
                    prompt_tokens=tokens["prompt_tokens"],
                    completion_tokens=tokens["completion_tokens"],
                    endpoint_called=self.endpoint,
                    metadata={
                        "langchain_callback": True,
                        "cache_read_tokens": tokens.get("cache_read_tokens", 0),
                        "cache_creation_tokens": tokens.get("cache_creation_tokens", 0),
                        "reasoning_tokens": tokens.get("reasoning_tokens", 0),
                        **costs,
                    },
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
