"""
Módulo híbrido para contagem de tokens - combinando tiktoken + response.usage
Integrado com Admin Center Service e cotação USD/BRL em tempo real
VERSÃO CORRIGIDA - Foco em OpenAI com extração precisa de tokens
"""
import logging
import requests
from typing import Dict, Any, List, Optional, Union
from datetime import datetime, timedelta
from decimal import Decimal
import tiktoken

try:
    from langchain.callbacks.base import BaseCallbackHandler
    try:
        from langchain_community.callbacks.manager import get_openai_callback
    except ImportError:
        from langchain.callbacks import get_openai_callback
    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False
    logging.warning("LangChain não disponível. Funcionalidades relacionadas serão desabilitadas.")

from automaxia_utils.admin_center.service import get_admin_center_service

class CurrencyService:
    """Serviço para obter cotação USD/BRL atualizada"""
    
    def __init__(self):
        self._cached_rate = None
        self._cache_timestamp = None
        self._cache_duration = timedelta(minutes=30)
        
    def get_usd_to_brl_rate(self) -> float:
        """Obtém a cotação USD/BRL atual com cache"""
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
            
            data = response.json()
            rate = data['rates']['BRL']
            
            self._cached_rate = rate
            self._cache_timestamp = now
            
            logging.info(f"Cotação USD/BRL atualizada: {rate:.4f}")
            return rate
            
        except Exception as e:
            logging.warning(f"Erro ao obter cotação USD/BRL: {e}")
            fallback_rate = 5.0
            if not self._cached_rate:
                self._cached_rate = fallback_rate
            return self._cached_rate

currency_service = CurrencyService()

def count_tokens_tiktoken(text: str, model: str = "gpt-3.5-turbo") -> int:
    """
    Conta tokens usando tiktoken (para estimativas apenas)
    ATENÇÃO: Esta é uma ESTIMATIVA. Use sempre response.usage quando disponível.
    """
    try:
        encoding = tiktoken.encoding_for_model(model)
        return len(encoding.encode(text))
    except Exception as e:
        logging.warning(f"Erro ao contar tokens com tiktoken: {e}")
        return len(text) // 4

def extract_tokens_from_response(response: Any) -> Optional[Dict[str, int]]:
    """
    Extrai tokens REAIS da resposta da API (método PRECISO)
    CORRIGIDO: Prioriza OpenAI, com fallbacks para outros providers
    """
    try:
        # 1. OpenAI SDK v1.x+ (formato atual - PRIORIDADE)
        if hasattr(response, 'usage'):
            usage = response.usage
            
            # Verificar se é o formato OpenAI
            if hasattr(usage, 'prompt_tokens'):
                tokens = {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens
                }
                logging.debug(f"✅ Tokens extraídos da OpenAI: {tokens}")
                return tokens
            
            # Formato Anthropic (fallback)
            if hasattr(usage, 'input_tokens'):
                tokens = {
                    "prompt_tokens": usage.input_tokens,
                    "completion_tokens": usage.output_tokens,
                    "total_tokens": usage.input_tokens + usage.output_tokens
                }
                logging.debug(f"✅ Tokens extraídos da Anthropic: {tokens}")
                return tokens
        
        # 2. LangChain response format
        if hasattr(response, 'llm_output') and isinstance(response.llm_output, dict):
            if 'token_usage' in response.llm_output:
                usage = response.llm_output['token_usage']
                tokens = {
                    "prompt_tokens": usage.get('prompt_tokens', 0),
                    "completion_tokens": usage.get('completion_tokens', 0),
                    "total_tokens": usage.get('total_tokens', 0)
                }
                logging.debug(f"✅ Tokens extraídos do LangChain: {tokens}")
                return tokens
        
        # 3. Formato dict (algumas APIs antigas)
        if isinstance(response, dict) and 'usage' in response:
            usage = response['usage']
            if 'prompt_tokens' in usage:
                tokens = {
                    "prompt_tokens": usage['prompt_tokens'],
                    "completion_tokens": usage['completion_tokens'],
                    "total_tokens": usage['total_tokens']
                }
                logging.debug(f"✅ Tokens extraídos de dict: {tokens}")
                return tokens
        
        # Se chegou aqui, não conseguiu extrair
        logging.warning(
            f"⚠️ Não foi possível extrair tokens do response. "
            f"Tipo: {type(response)}, "
            f"Atributos: {dir(response)[:5]}..."
        )
        return None
        
    except Exception as e:
        logging.error(f"❌ Erro ao extrair tokens da resposta: {e}")
        return None

class HybridTokenCounter:
    """
    Contador híbrido CORRIGIDO
    Busca preços dos modelos diretamente da API do Admin Center
    """
    
    # Preços fallback atualizados (Jan 2025) por 1K tokens em USD
    FALLBACK_PRICES_USD = {
        "gpt-4": {"input": 0.03, "output": 0.06},
        "gpt-4-turbo": {"input": 0.01, "output": 0.03},
        "gpt-4-turbo-preview": {"input": 0.01, "output": 0.03},
        "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
        "gpt-3.5-turbo-0125": {"input": 0.0005, "output": 0.0015},
        "claude-3-sonnet": {"input": 0.003, "output": 0.015},
        "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
        "claude-3-opus": {"input": 0.015, "output": 0.075}
    }
    
    def __init__(self, model: str = "gpt-3.5-turbo"):
        self.model = model
        self.admin_center = get_admin_center_service()
        self._model_cache = {}
        
    def _get_model_prices_from_api(self, model_name: str) -> Optional[Dict[str, float]]:
        """Busca preços do modelo na API do Admin Center"""
        try:
            if not self.admin_center.config.enabled:
                logging.debug("Admin Center desabilitado, usando preços fallback")
                return None
            
            endpoint = "/ai-model/consulta_nome"
            params = {"name": model_name}
            
            response = self.admin_center._make_request("GET", endpoint, params=params)
            
            if response and "data" in response:
                model_data = response["data"]
                
                input_cost = model_data.get("input_cost_per_token")
                output_cost = model_data.get("output_cost_per_token")
                
                if input_cost is not None and output_cost is not None:
                    return {
                        "input": float(input_cost) * 1000,
                        "output": float(output_cost) * 1000
                    }
            
            logging.debug(f"Modelo '{model_name}' não encontrado na API")
            return None
            
        except Exception as e:
            logging.error(f"Erro ao buscar preços do modelo '{model_name}' na API: {e}")
            return None
    
    def _get_model_prices(self, model_name: str) -> Dict[str, float]:
        """Obtém preços do modelo com cache inteligente"""
        if model_name in self._model_cache:
            cached_data = self._model_cache[model_name]
            cache_age = datetime.now() - cached_data["timestamp"]
            
            if cache_age < timedelta(hours=1):
                return cached_data["prices"]
        
        api_prices = self._get_model_prices_from_api(model_name)
        
        if api_prices:
            self._model_cache[model_name] = {
                "prices": api_prices,
                "timestamp": datetime.now(),
                "source": "api"
            }
            logging.info(
                f"Preços obtidos da API para '{model_name}': "
                f"input=${api_prices['input']:.6f}, output=${api_prices['output']:.6f} por 1K tokens"
            )
            return api_prices
        
        fallback_key = model_name if model_name in self.FALLBACK_PRICES_USD else "gpt-3.5-turbo"
        fallback_prices = self.FALLBACK_PRICES_USD[fallback_key]
        
        self._model_cache[model_name] = {
            "prices": fallback_prices,
            "timestamp": datetime.now() - timedelta(minutes=45),
            "source": "fallback"
        }
        
        logging.warning(
            f"Usando preços fallback para '{model_name}': "
            f"input=${fallback_prices['input']:.6f}, output=${fallback_prices['output']:.6f} por 1K tokens"
        )
        return fallback_prices

    def calculate_costs(self, prompt_tokens: int, completion_tokens: int) -> Dict[str, Any]:
        """Calcula custos em USD e BRL usando preços da API"""
        prices = self._get_model_prices(self.model)
        
        cost_input_usd = (prompt_tokens / 1000) * prices["input"]
        cost_output_usd = (completion_tokens / 1000) * prices["output"]
        total_cost_usd = cost_input_usd + cost_output_usd
        
        exchange_rate = currency_service.get_usd_to_brl_rate()
        total_cost_brl = total_cost_usd * exchange_rate
        
        price_source = self._model_cache.get(self.model, {}).get("source", "unknown")
        
        return {
            "cost_usd": round(total_cost_usd, 6),
            "cost_brl": round(total_cost_brl, 4),
            "exchange_rate": exchange_rate,
            "price_source": price_source,
            "cost_breakdown": {
                "input_usd": round(cost_input_usd, 6),
                "output_usd": round(cost_output_usd, 6),
                "input_brl": round(cost_input_usd * exchange_rate, 4),
                "output_brl": round(cost_output_usd * exchange_rate, 4)
            }
        }

# ==================== FUNÇÃO PRINCIPAL (RECOMENDADA) ====================
"""
Função UNIVERSAL para tracking - detecta automaticamente o provider
Adicione isso ao seu token_counter.py
"""
def track_api_response(
    response: Any, 
    model: str, 
    endpoint: str = "/api_direct", 
    user_id: Optional[str] = None, 
    prompt_text: str = "",
    force_provider: Optional[str] = None  # Novo parâmetro opcional
) -> Dict[str, Any]:
    """
    ✅ FUNÇÃO UNIVERSAL: Detecta automaticamente OpenAI, LangChain, Anthropic, etc
    
    Args:
        response: Objeto COMPLETO da resposta (qualquer provider)
        model: Nome do modelo usado
        endpoint: Endpoint da sua aplicação
        user_id: ID do usuário (opcional)
        prompt_text: Texto do prompt (apenas para metadata)
        force_provider: Forçar detecção específica ("openai", "langchain", "anthropic")
    
    Returns:
        Dict com tokens, custos e status do tracking
    """
    counter = HybridTokenCounter(model)
    
    # 1. DETECÇÃO AUTOMÁTICA DO PROVIDER
    provider = force_provider or _detect_provider(response)
    
    # 2. EXTRAÇÃO DE TOKENS BASEADA NO PROVIDER
    tokens = None
    source = "unknown"
    
    if provider == "openai":
        tokens = _extract_openai_tokens(response)
        source = "openai_api" if tokens else "tiktoken_fallback"
        
    elif provider == "langchain":
        tokens = _extract_langchain_tokens(response)
        source = "langchain_api" if tokens else "tiktoken_fallback"
        
    elif provider == "anthropic":
        tokens = _extract_anthropic_tokens(response)
        source = "anthropic_api" if tokens else "tiktoken_fallback"
        
    else:
        # Fallback genérico: tentar extração universal
        tokens = extract_tokens_from_response(response)
        source = "generic_extraction" if tokens else "tiktoken_fallback"
    
    # 3. FALLBACK PARA TIKTOKEN SE NECESSÁRIO
    if not tokens:
        logging.warning(
            f"Não foi possível extrair tokens de {provider}. "
            f"Usando tiktoken (menos preciso). Provider detectado: {provider}"
        )
        prompt_tokens = count_tokens_tiktoken(prompt_text, model)
        
        # Tentar extrair texto da resposta
        response_text = _extract_response_text(response, provider)
        completion_tokens = count_tokens_tiktoken(response_text, model) if response_text else 0
        
        tokens = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        }
        source = "tiktoken_fallback"
    
    prompt_tokens = tokens["prompt_tokens"]
    completion_tokens = tokens["completion_tokens"]
    
    logging.info(
        f"✅ Tokens extraídos via {source} (provider: {provider}): "
        f"prompt={prompt_tokens}, completion={completion_tokens}"
    )
    
    # 4. CALCULAR CUSTOS
    costs = counter.calculate_costs(prompt_tokens, completion_tokens)
    
    # 5. METADATA ENRIQUECIDO
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
    
    # 6. ENVIAR PARA ADMIN CENTER
    track_success = counter.admin_center.track_token_usage(
        model_name=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        endpoint_called=endpoint,
        user_id=user_id,
        metadata=enhanced_metadata
    )
    
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "source": source,
        "provider": provider,
        "model": model,
        "admin_center_tracked": track_success,
        "timestamp": datetime.now().isoformat(),
        **costs
    }


# ============================================================
# FUNÇÕES AUXILIARES DE DETECÇÃO E EXTRAÇÃO
# ============================================================

def _detect_provider(response: Any) -> str:
    """
    Detecta automaticamente o provider baseado na estrutura do objeto
    """
    response_type = type(response).__name__
    module = type(response).__module__
    
    # OpenAI SDK
    if "openai" in module.lower():
        return "openai"
    
    # LangChain
    if "langchain" in module.lower():
        return "langchain"
    
    # Anthropic
    if "anthropic" in module.lower():
        return "anthropic"
    
    # Detecção por estrutura
    if hasattr(response, "usage"):
        if hasattr(response.usage, "prompt_tokens"):
            return "openai"  # Formato OpenAI
        elif hasattr(response.usage, "input_tokens"):
            return "anthropic"  # Formato Anthropic
    
    if hasattr(response, "llm_output"):
        return "langchain"
    
    logging.warning(
        f"Provider desconhecido. Type: {response_type}, Module: {module}"
    )
    return "unknown"


def _extract_openai_tokens(response: Any) -> Optional[Dict[str, int]]:
    """Extrai tokens de resposta OpenAI"""
    try:
        if hasattr(response, "usage"):
            usage = response.usage
            return {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens
            }
    except Exception as e:
        logging.debug(f"Erro ao extrair tokens OpenAI: {e}")
    return None


def _extract_langchain_tokens(response: Any) -> Optional[Dict[str, int]]:
    """Extrai tokens de resposta LangChain"""
    try:
        # LangChain armazena em llm_output
        if hasattr(response, "llm_output") and isinstance(response.llm_output, dict):
            usage = response.llm_output.get("token_usage", {})
            if usage:
                return {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0)
                }
        
        # Algumas versões do LangChain expõem usage diretamente
        if hasattr(response, "usage"):
            return _extract_openai_tokens(response)
            
    except Exception as e:
        logging.debug(f"Erro ao extrair tokens LangChain: {e}")
    return None


def _extract_anthropic_tokens(response: Any) -> Optional[Dict[str, int]]:
    """Extrai tokens de resposta Anthropic (Claude)"""
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
        logging.debug(f"Erro ao extrair tokens Anthropic: {e}")
    return None


def _extract_response_text(response: Any, provider: str) -> str:
    """Extrai texto da resposta baseado no provider"""
    try:
        # OpenAI
        if provider == "openai":
            if hasattr(response, "choices") and response.choices:
                return response.choices[0].message.content or ""
        
        # LangChain
        elif provider == "langchain":
            if hasattr(response, "text"):
                return response.text
            if hasattr(response, "content"):
                return response.content
            if hasattr(response, "generations"):
                return response.generations[0][0].text
        
        # Anthropic
        elif provider == "anthropic":
            if hasattr(response, "content"):
                if isinstance(response.content, list):
                    return " ".join([c.text for c in response.content if hasattr(c, "text")])
                return response.content
        
        # Genérico
        if hasattr(response, "content"):
            return str(response.content)
        if hasattr(response, "text"):
            return str(response.text)
            
    except Exception as e:
        logging.debug(f"Erro ao extrair texto da resposta: {e}")
    
    return ""
# ==================== FUNÇÕES DE COMPATIBILIDADE ====================

def track_openai_call(
    prompt: str, 
    response: str, 
    model: str = "gpt-3.5-turbo", 
    endpoint: str = "/openai_direct", 
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    ⚠️ DEPRECATED: Use track_api_response() com o objeto completo
    
    Mantido para compatibilidade com código legado.
    ATENÇÃO: Usa apenas tiktoken (estimativa), pode não bater com OpenAI!
    """
    logging.warning(
        "⚠️ track_openai_call() está deprecated. "
        "Use track_api_response() passando o objeto completo para maior precisão!"
    )
    
    counter = HybridTokenCounter(model)
    
    prompt_tokens = count_tokens_tiktoken(prompt, model)
    completion_tokens = count_tokens_tiktoken(response, model)
    
    costs = counter.calculate_costs(prompt_tokens, completion_tokens)
    
    enhanced_metadata = {
        "prompt_text": prompt[:500],
        "model_name": model,
        "vlr_dolar": costs["exchange_rate"],
        "cost_usd": costs["cost_usd"],
        "cost_brl": costs["cost_brl"],
        "price_source": costs["price_source"],
        "cost_breakdown": costs["cost_breakdown"],
        "token_source": "tiktoken_legacy",
        "timestamp": datetime.now().isoformat()
    }
    
    track_success = counter.admin_center.track_token_usage(
        model_name=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        endpoint_called=endpoint,
        user_id=user_id,
        metadata=enhanced_metadata
    )
    
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "source": "tiktoken_legacy",
        "model": model,
        "admin_center_tracked": track_success,
        "timestamp": datetime.now().isoformat(),
        **costs
    }

def estimate_tokens_and_cost(
    prompt: str, 
    model: str = "gpt-3.5-turbo", 
    estimated_response_length: int = 100
) -> Dict[str, Any]:
    """Para estimativas prévias (antes de chamar a API)"""
    counter = HybridTokenCounter(model)
    
    prompt_tokens = count_tokens_tiktoken(prompt, model)
    completion_tokens = estimated_response_length
    
    costs = counter.calculate_costs(prompt_tokens, completion_tokens)
    
    return {
        "prompt_tokens": prompt_tokens,
        "estimated_completion_tokens": completion_tokens,
        "estimated_total_tokens": prompt_tokens + completion_tokens,
        "source": "estimation",
        "model": model,
        **costs
    }

# ==================== LANGCHAIN CALLBACK ====================

if LANGCHAIN_AVAILABLE:
    class LangChainTokenCallback(BaseCallbackHandler):
        """Callback otimizado para LangChain"""
        
        def __init__(self, model: str = "gpt-3.5-turbo", endpoint: str = "/langchain"):
            super().__init__()
            self.model = model
            self.endpoint = endpoint
            self.admin_center = get_admin_center_service()
            self.counter = HybridTokenCounter(model)
            
        def on_llm_end(self, response: Any, **kwargs) -> None:
            """Processa tokens ao final da chamada LangChain"""
            try:
                tokens = extract_tokens_from_response(response)
                
                if not tokens:
                    logging.warning("Não foi possível extrair tokens do LangChain callback")
                    return
                
                prompt_tokens = tokens["prompt_tokens"]
                completion_tokens = tokens["completion_tokens"]
                
                costs = self.counter.calculate_costs(prompt_tokens, completion_tokens)
                
                enhanced_metadata = {
                    "langchain_callback": True,
                    "model_name": self.model,
                    "vlr_dolar": costs["exchange_rate"],
                    "cost_usd": costs["cost_usd"],
                    "cost_brl": costs["cost_brl"],
                    "price_source": costs["price_source"],
                    "cost_breakdown": costs["cost_breakdown"],
                    "timestamp": datetime.now().isoformat()
                }
                
                self.admin_center.track_token_usage(
                    model_name=self.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    endpoint_called=self.endpoint,
                    metadata=enhanced_metadata
                )
                
                logging.info(
                    f"LangChain tokens tracked: {prompt_tokens}+{completion_tokens}, "
                    f"custo: ${costs['cost_usd']:.6f} USD / R${costs['cost_brl']:.4f} BRL"
                )
                
            except Exception as e:
                logging.error(f"Erro no callback LangChain: {e}")
else:
    class LangChainTokenCallback:
        def __init__(self, model: str = "gpt-3.5-turbo", endpoint: str = "/langchain"):
            logging.warning("LangChain não disponível. Callback desabilitado.")
        
        def on_llm_end(self, response: Any, **kwargs) -> None:
            pass

# Função para compatibilidade com código legado
count_tokens = count_tokens_tiktoken

def invalidate_model_price_cache(model_name: str = None):
    """Invalida cache de preços de modelo específico ou todos"""
    logging.info(f"Solicitação para invalidar cache de preços: {model_name or 'todos os modelos'}")