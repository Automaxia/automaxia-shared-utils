"""
Automaxia Utils - Pacote compartilhado
"""

__version__ = "1.0.0"
__author__ = "Automaxia"

# Importar de admin_center
from .admin_center import (
    AdminCenterService,
    AdminCenterConfig,
    get_admin_center_service,
    reset_admin_center_service,
    AdminCenterContext,
    track_execution
)

# Importar de token_tracking
from .token_tracking import (
    track_api_response,
    track_openai_call,
    estimate_tokens_and_cost,
    count_tokens_tiktoken,
    extract_tokens_from_response,
    HybridTokenCounter,
    LangChainTokenCallback,
    invalidate_model_price_cache
)

__all__ = [
    # Admin Center
    "AdminCenterService",
    "AdminCenterConfig",
    "get_admin_center_service",
    "reset_admin_center_service",
    "AdminCenterContext",
    "track_execution",
    
    # Token Tracking
    "track_api_response",
    "track_openai_call",
    "estimate_tokens_and_cost",
    "count_tokens_tiktoken",
    "extract_tokens_from_response",
    "HybridTokenCounter",
    "LangChainTokenCallback",
    "invalidate_model_price_cache",
]