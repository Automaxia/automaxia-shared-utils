"""
Automaxia Utils - Pacote compartilhado
"""

__version__ = "1.5.0"
__author__ = "Automaxia"

# Importar de admin_center
from .admin_center import (
    AdminCenterService,
    AdminCenterConfig,
    get_admin_center_service,
    reset_admin_center_service,
    AdminCenterContext,
    track_execution,
    JobRunner,
    ResolvedConnection,
    ConnectionResolver,
)

# Importar de token_tracking
from .token_tracking import (
    track_api_response,
    track_openai_call,
    estimate_tokens_and_cost,
    count_tokens_tiktoken,
    count_tokens_litellm,
    count_tokens_smart,
    extract_tokens_from_response,
    HybridTokenCounter,
    LangChainTokenCallback,
    invalidate_model_price_cache
)

# Importar de auth — depende de FastAPI, que e' opcional. Produtos que sao
# clientes (ex.: ischolar, RPAs) nao precisam de FastAPI; o auth/middleware so
# faz sentido em servicos que expoem API HTTP. Se nao estiver instalado,
# pula silenciosamente em vez de quebrar o import do pacote inteiro.
try:
    from .auth import (
        AdminCenterAuth,
        AdminCenterAuthConfig,
        get_current_user as get_authenticated_user,
        require_product_access,
        login_via_admincenter,
    )
    _AUTH_AVAILABLE = True
except ImportError:
    _AUTH_AVAILABLE = False

__all__ = [
    # Admin Center
    "AdminCenterService",
    "AdminCenterConfig",
    "get_admin_center_service",
    "reset_admin_center_service",
    "AdminCenterContext",
    "track_execution",
    "JobRunner",
    "ResolvedConnection",
    "ConnectionResolver",

    # Token Tracking
    "track_api_response",
    "track_openai_call",
    "estimate_tokens_and_cost",
    "count_tokens_tiktoken",
    "count_tokens_litellm",
    "count_tokens_smart",
    "extract_tokens_from_response",
    "HybridTokenCounter",
    "LangChainTokenCallback",
    "invalidate_model_price_cache",
]

if _AUTH_AVAILABLE:
    __all__ += [
        "AdminCenterAuth",
        "AdminCenterAuthConfig",
        "get_authenticated_user",
        "require_product_access",
        "login_via_admincenter",
    ]