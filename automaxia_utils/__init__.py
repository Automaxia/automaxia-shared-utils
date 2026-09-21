"""
Automaxia Utils - Pacote compartilhado
"""

__version__ = "1.17.0"
__author__ = "Automaxia"

# Importar de admin_center
from .admin_center import (
    AdminCenterService,
    AdminCenterConfig,
    AdminCenterEndpoints,
    get_admin_center_service,
    reset_admin_center_service,
    AdminCenterContext,
    track_execution,
    JobRunner,
    JobCancelled,
    ResolvedConnection,
    ConnectionResolver,
    build_bigquery_client,
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
    invalidate_model_price_cache,
    # Contexto de agente do token tracking. Existia so dentro de
    # token_tracking; sem reexportar aqui, `from automaxia_utils import
    # definir_agente` quebra — e e assim que o Balance importa.
    definir_agente,
    agente_atual,
    agente_em_uso,
)

# Importar de registration (auto-registro de produtos no AdminCenter)
from .registration import (
    ProductManifest,
    ProductRegistrationConfig,
    register_with_platform,
    send_heartbeat,
    start_heartbeat_loop,
)

# Migrations: alembic upgrade com retry, para o lifespan dos backends.
# Import protegido — alembic e' dependencia so' de quem tem banco proprio
# (RPAs e clientes puros nao instalam).
try:
    from .migrations import run_migrations
    _MIGRATIONS_AVAILABLE = True
except ImportError:
    _MIGRATIONS_AVAILABLE = False

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
        has_permission,
        has_any_permission,
        enrich_user_with_permissions,
        require_permission,
        require_any_permission,
        invalidate_permission_cache,
    )
    _AUTH_AVAILABLE = True
except ImportError:
    _AUTH_AVAILABLE = False

__all__ = [
    # Admin Center
    "AdminCenterService",
    "AdminCenterConfig",
    "AdminCenterEndpoints",
    "get_admin_center_service",
    "reset_admin_center_service",
    "AdminCenterContext",
    "track_execution",
    "JobRunner",
    "JobCancelled",
    "ResolvedConnection",
    "ConnectionResolver",
    "build_bigquery_client",

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
    "definir_agente",
    "agente_atual",
    "agente_em_uso",

    # Registration (auto-registro de produtos)
    "ProductManifest",
    "ProductRegistrationConfig",
    "register_with_platform",
    "send_heartbeat",
    "start_heartbeat_loop",
]

if _MIGRATIONS_AVAILABLE:
    __all__ += ["run_migrations"]

if _AUTH_AVAILABLE:
    __all__ += [
        "AdminCenterAuth",
        "AdminCenterAuthConfig",
        "get_authenticated_user",
        "require_product_access",
        "login_via_admincenter",
        "has_permission",
        "has_any_permission",
        "enrich_user_with_permissions",
        "require_permission",
        "require_any_permission",
        "invalidate_permission_cache",
    ]