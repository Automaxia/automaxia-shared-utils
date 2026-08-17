"""
Auth Middleware Centralizado - Automaxia Shared Utils

Middleware reutilizavel para validar JWT do AdminCenter em qualquer projeto.
Permite que Dashboard, DataChatAI e outros projetos usem a mesma autenticacao
centralizada do AdminCenter (plataforma-backend).

Dois modos de operacao:
1. LOCAL: Valida JWT localmente usando SECRET_KEY compartilhada (rapido, sem rede)
2. REMOTE: Valida via chamada HTTP ao AdminCenter /auth/validate-token (mais seguro)

Uso basico:
    from automaxia_utils.auth import get_current_user, login_via_admincenter

    # Em qualquer endpoint FastAPI:
    @app.get("/dados")
    async def dados(user = Depends(get_current_user)):
        print(user.email, user.product_access)
"""

import os
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

import requests
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

logger = logging.getLogger(__name__)

security = HTTPBearer()


# =============================================
# CONFIG
# =============================================

@dataclass
class AdminCenterAuthConfig:
    """Configuracao do middleware de auth centralizado."""

    # URL da API do AdminCenter
    admincenter_url: str = ""

    # SECRET_KEY compartilhada (mesma do AdminCenter) para validacao local
    secret_key: str = ""
    algorithm: str = "HS256"

    # Slug do produto atual (ex: 'dashboard', 'datachatai')
    product_slug: str = ""

    # Se True, valida JWT localmente. Se False, chama AdminCenter API.
    local_validation: bool = True

    # Cache de usuarios (TTL em segundos)
    cache_ttl: int = 300  # 5 minutos

    @classmethod
    def from_env(cls):
        """Carrega config das variaveis de ambiente."""
        return cls(
            admincenter_url=os.getenv("ADMIN_CENTER_URL", ""),
            secret_key=os.getenv("SECRET_KEY", os.getenv("JWT_SECRET_KEY", "")),
            algorithm=os.getenv("ALGORITHM", "HS256"),
            product_slug=os.getenv("PRODUCT_SLUG", ""),
            local_validation=os.getenv("AUTH_LOCAL_VALIDATION", "true").lower() == "true",
            cache_ttl=int(os.getenv("AUTH_CACHE_TTL", "300")),
        )


def _gate_fail_open() -> bool:
    """Politica do gate de produto quando o AdminCenter esta indisponivel.

    Default False = fail-closed: se nao da para validar o acesso, NEGA
    (HTTP 503). Defina AUTH_PRODUCT_GATE_FAIL_OPEN=true para voltar ao
    comportamento antigo (liberar em falha de rede) — use com consciencia,
    apenas em ambientes onde disponibilidade importa mais que o gate.

    Lido do ambiente a cada chamada (barato) para permitir toggle em
    runtime/testes sem reiniciar o singleton.
    """
    return os.getenv("AUTH_PRODUCT_GATE_FAIL_OPEN", "false").strip().lower() in (
        "true", "1", "yes",
    )


# =============================================
# MODELS
# =============================================

class ProductAccess(BaseModel):
    """Acesso do usuario a um produto especifico."""
    product_id: Optional[str] = None
    product_slug: Optional[str] = None
    profile_name: Optional[str] = None
    permissions: Dict[str, Any] = {}
    is_active: bool = True


class AuthenticatedUser(BaseModel):
    """Usuario autenticado extraido do JWT do AdminCenter."""
    user_id: str
    email: str
    organization_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    status: Optional[str] = "active"
    product_access: Optional[ProductAccess] = None
    # Claims novos do AdminCenter — podem nao existir em tokens antigos,
    # por isso defaults seguros (sem privilegio).
    is_super_admin: bool = False
    permissions: Optional[List[str]] = None
    # Detalhe estruturado de permissoes resolvido via POST /auth/me/full:
    # {'organization_permissions': [...], 'products': {slug: [...]}}.
    # Preenchido por enrich_user_with_permissions; None quando as permissoes
    # vieram apenas da claim flat do JWT (sem escopo por produto).
    permission_detail: Optional[Dict[str, Any]] = None
    raw_claims: Dict[str, Any] = {}


# =============================================
# AUTH SERVICE
# =============================================

class AdminCenterAuth:
    """
    Servico de autenticacao centralizada via AdminCenter.

    Valida tokens JWT emitidos pelo AdminCenter e opcionalmente
    verifica se o usuario tem acesso ao produto atual.
    """

    def __init__(self, config: AdminCenterAuthConfig = None):
        self.config = config or AdminCenterAuthConfig.from_env()
        self._user_cache: Dict[str, Dict[str, Any]] = {}
        self._session: Optional[requests.Session] = None

        if not self.config.secret_key and self.config.local_validation:
            logger.warning(
                "SECRET_KEY nao configurada para validacao local. "
                "Defina SECRET_KEY ou JWT_SECRET_KEY no .env"
            )

    def _get_session(self) -> requests.Session:
        """Session HTTP reutilizavel."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"Content-Type": "application/json"})
        return self._session

    def _get_cached_user(self, cache_key: str) -> Optional[AuthenticatedUser]:
        """Busca usuario no cache local."""
        if cache_key in self._user_cache:
            cached = self._user_cache[cache_key]
            if time.time() - cached["timestamp"] < self.config.cache_ttl:
                return cached["user"]
            del self._user_cache[cache_key]
        return None

    def _cache_user(self, cache_key: str, user: AuthenticatedUser):
        """Armazena usuario no cache."""
        self._user_cache[cache_key] = {
            "user": user,
            "timestamp": time.time(),
        }

    def validate_token_local(self, token: str) -> Optional[AuthenticatedUser]:
        """
        Valida JWT localmente usando SECRET_KEY compartilhada.
        Rapido, sem chamada de rede. Requer mesma SECRET_KEY do AdminCenter.
        """
        try:
            from jose import jwt, JWTError
        except ImportError:
            try:
                import jwt as pyjwt
                # Fallback para PyJWT
                try:
                    payload = pyjwt.decode(
                        token,
                        self.config.secret_key,
                        algorithms=[self.config.algorithm]
                    )
                except pyjwt.ExpiredSignatureError:
                    logger.info("Token expirado (PyJWT)")
                    return None
                except pyjwt.InvalidTokenError as e:
                    logger.warning(f"Token invalido (PyJWT): {e}")
                    return None

                return self._payload_to_user(payload)
            except ImportError:
                logger.error("Nenhuma lib JWT disponivel. Instale python-jose ou PyJWT.")
                return None

        try:
            payload = jwt.decode(
                token,
                self.config.secret_key,
                algorithms=[self.config.algorithm],
            )
            return self._payload_to_user(payload)
        except jwt.ExpiredSignatureError:
            logger.info("Token expirado")
            return None
        except JWTError as e:
            logger.warning(f"Token invalido: {e}")
            return None

    def validate_token_remote(self, token: str) -> Optional[AuthenticatedUser]:
        """
        Valida token via chamada HTTP ao AdminCenter.
        Mais seguro (verifica status do usuario no banco), mas mais lento.

        Retorna None quando o AdminCenter NEGA (401/403 — inclusive HTTP 200
        com envelope {success: false, status_code: 403}, comportamento do
        backend legado).

        Quando o AdminCenter esta INDISPONIVEL (5xx, timeout, conexao
        recusada), NAO libera: levanta HTTPException 503 (fail-closed).
        Com AUTH_PRODUCT_GATE_FAIL_OPEN=true, degrada para validacao local
        do JWT (se SECRET_KEY configurada) em vez de negar.
        """
        if not self.config.admincenter_url:
            logger.error("ADMIN_CENTER_URL nao configurada para validacao remota")
            return None

        url = f"{self.config.admincenter_url}/auth/validate-product-access"
        unavailable_reason: Optional[str] = None

        try:
            session = self._get_session()
            response = session.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"product_slug": self.config.product_slug} if self.config.product_slug else {},
                timeout=10,
            )

            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                if data.get("success"):
                    user_data = data.get("data", {})
                    permissions = user_data.get("permissions")
                    user = AuthenticatedUser(
                        user_id=user_data.get("user_id", ""),
                        email=user_data.get("email", ""),
                        organization_id=user_data.get("organization_id"),
                        first_name=user_data.get("first_name"),
                        last_name=user_data.get("last_name"),
                        status=user_data.get("status", "active"),
                        is_super_admin=bool(user_data.get("is_super_admin", False)),
                        permissions=list(permissions) if isinstance(permissions, list) else None,
                        raw_claims=user_data,
                    )

                    # Adicionar acesso ao produto se retornado
                    if "product_access" in user_data:
                        pa = user_data["product_access"]
                        user.product_access = ProductAccess(
                            product_id=pa.get("product_id"),
                            product_slug=pa.get("product_slug"),
                            profile_name=pa.get("profile_name"),
                            permissions=pa.get("permissions", {}),
                            is_active=pa.get("is_active", True),
                        )

                    return user

                # Envelope legado: HTTP 200 com success=false. O status real
                # vem em body.status_code.
                envelope_status = int(data.get("status_code") or 403)
                if envelope_status in (401, 403):
                    logger.warning(
                        "Acesso negado pelo AdminCenter (envelope status=%s)",
                        envelope_status,
                    )
                    return None
                unavailable_reason = f"envelope success=false status={envelope_status}"
            elif response.status_code == 401:
                logger.info("Token rejeitado pelo AdminCenter (401)")
                return None
            elif response.status_code == 403:
                logger.warning("Acesso negado ao produto pelo AdminCenter (403)")
                return None
            else:
                unavailable_reason = f"status={response.status_code}"

        except requests.RequestException as e:
            unavailable_reason = str(e)

        # AdminCenter indisponivel: NAO liberar por default (fail-closed).
        logger.warning(
            "Validacao remota indisponivel no AdminCenter (%s).", unavailable_reason
        )
        if _gate_fail_open():
            if self.config.secret_key:
                logger.warning(
                    "AUTH_PRODUCT_GATE_FAIL_OPEN=true — degradando para "
                    "validacao local do JWT."
                )
                return self.validate_token_local(token)
            logger.warning(
                "AUTH_PRODUCT_GATE_FAIL_OPEN=true mas SECRET_KEY ausente — "
                "impossivel validar; negando."
            )
            return None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Validacao de acesso indisponivel. Tente novamente.",
        )

    def validate_token(self, token: str) -> Optional[AuthenticatedUser]:
        """
        Valida token usando o modo configurado (local ou remoto).
        Usa cache para evitar revalidacoes desnecessarias.
        """
        # Verificar cache
        cache_key = f"token_{hash(token)}"
        cached = self._get_cached_user(cache_key)
        if cached:
            return cached

        # Validar
        if self.config.local_validation:
            user = self.validate_token_local(token)
        else:
            user = self.validate_token_remote(token)

        # Cachear resultado
        if user:
            self._cache_user(cache_key, user)

        return user

    def _payload_to_user(self, payload: Dict[str, Any]) -> AuthenticatedUser:
        """Converte payload JWT em AuthenticatedUser.

        Claims `is_super_admin` e `permissions` podem nao existir em tokens
        emitidos por versoes antigas do AdminCenter — defaults seguros
        (False / None) nesse caso.
        """
        permissions = payload.get("permissions")
        return AuthenticatedUser(
            user_id=payload.get("user_id", ""),
            email=payload.get("sub", ""),
            organization_id=payload.get("organization_id"),
            status="active",
            is_super_admin=bool(payload.get("is_super_admin", False)),
            permissions=list(permissions) if isinstance(permissions, list) else None,
            raw_claims=payload,
        )

    def invalidate_cache(self, email: str = None):
        """Invalida cache de usuario."""
        if email:
            keys_to_remove = [
                k for k, v in self._user_cache.items()
                if v.get("user", {}).email == email
            ]
            for k in keys_to_remove:
                del self._user_cache[k]
        else:
            self._user_cache.clear()


# =============================================
# SINGLETON
# =============================================

_auth_instance: Optional[AdminCenterAuth] = None


def _get_auth() -> AdminCenterAuth:
    """Retorna instancia singleton do AdminCenterAuth."""
    global _auth_instance
    if _auth_instance is None:
        _auth_instance = AdminCenterAuth()
    return _auth_instance


def configure_auth(config: AdminCenterAuthConfig):
    """Configura o singleton de auth com config customizada."""
    global _auth_instance
    _auth_instance = AdminCenterAuth(config)


# =============================================
# FASTAPI DEPENDENCIES
# =============================================

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> AuthenticatedUser:
    """
    FastAPI Dependency - Extrai e valida usuario do token JWT.

    Uso:
        @app.get("/dados")
        async def dados(user: AuthenticatedUser = Depends(get_current_user)):
            print(user.email)
    """
    auth = _get_auth()
    token = credentials.credentials

    user = auth.validate_token(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalido ou expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def require_product_access(product_slug: str = None):
    """
    FastAPI Dependency Factory - Verifica se usuario tem acesso a um produto.

    Uso:
        @app.get("/dashboard/data")
        async def data(user = Depends(require_product_access("dashboard"))):
            print(user.email, user.product_access.profile_name)
    """
    async def _dependency(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> AuthenticatedUser:
        auth = _get_auth()
        slug = product_slug or auth.config.product_slug

        token = credentials.credentials

        # Se validacao remota, o AdminCenter ja verifica acesso ao produto
        if not auth.config.local_validation:
            # Forcar product_slug na validacao remota
            original_slug = auth.config.product_slug
            auth.config.product_slug = slug
            user = auth.validate_token_remote(token)
            auth.config.product_slug = original_slug

            if not user:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Acesso negado ao produto '{slug}'",
                )
            return user

        # Validacao local: valida token e depois verifica acesso via AdminCenter
        user = auth.validate_token_local(token)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token invalido ou expirado",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Verificar acesso ao produto via AdminCenter API
        if slug and auth.config.admincenter_url:
            denied_status: Optional[int] = None
            unavailable_reason: Optional[str] = None
            try:
                session = auth._get_session()
                response = session.post(
                    f"{auth.config.admincenter_url}/auth/validate-product-access",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"product_slug": slug},
                    timeout=10,
                )
                if response.status_code == 200:
                    try:
                        data = response.json()
                    except ValueError:
                        data = {}
                    if data.get("success"):
                        if "product_access" in data.get("data", {}):
                            pa = data["data"]["product_access"]
                            user.product_access = ProductAccess(
                                product_id=pa.get("product_id"),
                                product_slug=pa.get("product_slug"),
                                profile_name=pa.get("profile_name"),
                                permissions=pa.get("permissions", {}),
                                is_active=pa.get("is_active", True),
                            )
                    else:
                        # Envelope legado: HTTP 200 com {success: false,
                        # status_code: 403} conta como negado.
                        envelope_status = int(data.get("status_code") or 403)
                        if envelope_status in (401, 403):
                            denied_status = envelope_status
                        else:
                            unavailable_reason = (
                                f"envelope success=false status={envelope_status}"
                            )
                elif response.status_code in (401, 403):
                    denied_status = response.status_code
                else:
                    # 5xx ou qualquer status inesperado: validacao indisponivel.
                    unavailable_reason = f"status={response.status_code}"
            except HTTPException:
                raise
            except Exception as e:
                unavailable_reason = str(e)

            if denied_status is not None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Acesso negado ao produto '{slug}'",
                )

            if unavailable_reason is not None:
                logger.warning(
                    "Nao foi possivel validar acesso ao produto '%s' no "
                    "AdminCenter (%s).", slug, unavailable_reason,
                )
                if not _gate_fail_open():
                    # Fail-closed (default): sem confirmacao do AdminCenter,
                    # NEGA em vez de liberar.
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Validacao de acesso indisponivel. Tente novamente.",
                    )
                logger.warning(
                    "AUTH_PRODUCT_GATE_FAIL_OPEN=true — liberando acesso ao "
                    "produto '%s' sem confirmacao do AdminCenter.", slug,
                )

        return user

    return _dependency


# =============================================
# LOGIN PROXY
# =============================================

def login_via_admincenter(
    email: str,
    password: str,
    product_slug: str = None,
    admincenter_url: str = None,
) -> Optional[Dict[str, Any]]:
    """
    Faz login no AdminCenter e retorna tokens + dados do usuario.

    Para uso em backends que querem oferecer login proprio (email/senha)
    mas validando no AdminCenter.

    Args:
        email: Email do usuario
        password: Senha do usuario
        product_slug: Slug do produto (para verificar acesso)
        admincenter_url: URL do AdminCenter (usa env se nao informado)

    Returns:
        Dict com access_token, refresh_token, user, organization
        ou None se login falhar

    Raises:
        HTTPException: Se credenciais invalidas ou sem acesso
    """
    auth = _get_auth()
    url = admincenter_url or auth.config.admincenter_url

    if not url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AdminCenter URL nao configurada",
        )

    try:
        # 1. Login no AdminCenter
        session = auth._get_session()
        login_response = session.post(
            f"{url}/auth/login",
            data={"username": email, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )

        if login_response.status_code != 200:
            body = login_response.json() if login_response.headers.get("content-type", "").startswith("application/json") else {}
            detail = body.get("message", "Email ou senha incorretos")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=detail,
            )

        login_data = login_response.json()

        # 2. Verificar acesso ao produto (se product_slug informado)
        slug = product_slug or auth.config.product_slug
        if slug:
            access_token = login_data.get("access_token")
            access_response = session.post(
                f"{url}/auth/validate-product-access",
                headers={"Authorization": f"Bearer {access_token}"},
                json={"product_slug": slug},
                timeout=10,
            )

            if access_response.status_code == 403:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Usuario nao tem acesso ao produto '{slug}'",
                )

            if access_response.status_code == 200:
                access_data = access_response.json()
                if access_data.get("success"):
                    login_data["product_access"] = access_data.get("data", {}).get("product_access")

        return login_data

    except HTTPException:
        raise
    except requests.RequestException as e:
        logger.error(f"Erro ao conectar ao AdminCenter: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AdminCenter indisponivel",
        )


# =============================================
# RBAC HELPERS (porte da v1.6 do Cockpit InfraBalance)
# =============================================
#
# O AdminCenter usa permissoes no formato `recurso:acao` (ex.:
# `users:manage`, `dashboards:read`). Cada usuario tem uma uniao de
# permissoes vindas de roles diretas + roles via grupos.
#
# Duas fontes possiveis de permissoes (em ordem):
#   1. Claim `permissions` do JWT — quando o AdminCenter passar a incluir.
#      Stateless, zero roundtrips (lista flat, sem escopo por produto).
#   2. Endpoint `POST /auth/me/full` do AdminCenter — fallback quando o JWT
#      nao carrega permissoes. Retorna bloco estruturado com
#      `organization_permissions` (valem para qualquer produto) e
#      `products[].permissions` (escopadas por produto). A lib mantem cache
#      TTL local para evitar N chamadas por janela curta.
#
# Super admins (`user.is_super_admin = True`) bypassam toda checagem.

# Cache de permissoes resolvidas via /auth/me/full. Chave = user_id.
_permission_cache: Dict[str, Dict[str, Any]] = {}
# TTL curto: mudanca de role ainda eh percebida em ate 60s.
_PERMISSION_CACHE_TTL_SECONDS = 60


def _fetch_permissions_from_me(token: str) -> Optional[Dict[str, Any]]:
    """Resolve permissoes via `POST /auth/me/full` do AdminCenter.

    Resposta esperada (envelope padrao):
        {success, data: {user_id, email, is_super_admin,
                         organization_permissions: [...],
                         products: [{product_id, product_slug, product_name,
                                     permissions: [...]}],
                         accessible_products}}

    Retorna dict estruturado:
        {
          'is_super_admin': bool,
          'organization_permissions': [str, ...],
          'products': {product_slug: [str, ...]},
          'merged': [str, ...],  # uniao deduplicada org + todos os produtos
        }

    Em caso de falha (rede, status != 200, envelope success=false),
    retorna None (caller decide o que fazer).
    """
    auth = _get_auth()
    base_url = auth.config.admincenter_url
    if not base_url:
        logger.warning("ADMIN_CENTER_URL nao configurada; /auth/me/full indisponivel")
        return None

    try:
        session = auth._get_session()
        response = session.post(
            f"{base_url}/auth/me/full",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if response.status_code != 200:
            logger.warning(
                f"/auth/me/full retornou {response.status_code}: {response.text[:200]}"
            )
            return None

        body = response.json()
        if not isinstance(body, dict) or body.get("success") is False:
            logger.warning("/auth/me/full: envelope com success=false")
            return None
        data = body.get("data")
        if not isinstance(data, dict):
            return None

        org_perms = [p for p in (data.get("organization_permissions") or []) if p]
        products: Dict[str, List[str]] = {}
        for prod in data.get("products") or []:
            slug = prod.get("product_slug")
            if slug:
                products[slug] = [p for p in (prod.get("permissions") or []) if p]

        # Uniao deduplicada preservando ordem.
        seen, merged = set(), []
        all_product_perms = [p for perms in products.values() for p in perms]
        for p in [*org_perms, *all_product_perms]:
            if p not in seen:
                seen.add(p)
                merged.append(p)

        return {
            "is_super_admin": bool(data.get("is_super_admin", False)),
            "organization_permissions": org_perms,
            "products": products,
            "merged": merged,
        }
    except requests.RequestException as e:
        logger.warning(f"Erro de rede ao buscar /auth/me/full: {e}")
        return None
    except Exception as e:
        logger.warning(f"Erro inesperado ao parsear /auth/me/full: {e}")
        return None


def _cached_permissions(user_id: str, token: str) -> Optional[Dict[str, Any]]:
    """Retorna bloco de permissoes do cache local ou faz roundtrip e popula."""
    if not user_id:
        return None
    entry = _permission_cache.get(user_id)
    if entry and (time.time() - entry["timestamp"]) < _PERMISSION_CACHE_TTL_SECONDS:
        return entry["detail"]

    detail = _fetch_permissions_from_me(token)
    if detail is not None:
        _permission_cache[user_id] = {
            "detail": detail,
            "timestamp": time.time(),
        }
    return detail


def invalidate_permission_cache(user_id: Optional[str] = None) -> None:
    """Invalida cache de permissoes. Sem argumento, limpa tudo."""
    if user_id:
        _permission_cache.pop(user_id, None)
    else:
        _permission_cache.clear()


def has_permission(
    user: AuthenticatedUser,
    permission: str,
    product_slug: Optional[str] = None,
) -> bool:
    """Verifica se o usuario possui uma permissao especifica.

    Sincrono e sem efeito colateral de rede. Pre-condicao: as permissoes
    do user devem ter sido resolvidas previamente (via claim do JWT ou
    via `enrich_user_with_permissions` / `require_permission` dependency).

    Regras:
    - `is_super_admin=True` libera tudo.
    - Permissao em `organization_permissions` vale para QUALQUER produto.
    - Com `product_slug`, permissoes de produto sao procuradas apenas no
      bloco daquele produto; sem `product_slug`, qualquer produto vale.
    - Quando so ha a lista flat (claim do JWT, sem `permission_detail`),
      o `product_slug` nao tem como ser aplicado e a checagem cai na flat.
    - `user.permissions is None` (nao resolvido) retorna False.

    Args:
        user: usuario autenticado (de `get_current_user` ou similar).
        permission: string `recurso:acao` (ex.: `'dashboards:read'`).
        product_slug: escopo de produto opcional.

    Returns:
        True se o user tem a permissao OU eh super admin; False caso
        contrario.
    """
    if user is None:
        return False
    if user.is_super_admin:
        return True

    detail = user.permission_detail
    if detail is not None:
        if permission in (detail.get("organization_permissions") or []):
            return True
        products = detail.get("products") or {}
        if product_slug is not None:
            return permission in (products.get(product_slug) or [])
        return any(permission in perms for perms in products.values())

    if user.permissions is None:
        return False
    return permission in user.permissions


def has_any_permission(
    user: AuthenticatedUser,
    permissions: List[str],
    product_slug: Optional[str] = None,
) -> bool:
    """Variante OR: retorna True se o user tiver pelo menos uma das permissoes."""
    if user is None:
        return False
    if user.is_super_admin:
        return True
    if not permissions:
        return False
    return any(has_permission(user, p, product_slug) for p in permissions)


def enrich_user_with_permissions(
    user: AuthenticatedUser,
    token: str,
) -> AuthenticatedUser:
    """Garante que `user.permissions` esta populado.

    1. Se o JWT ja trouxe `permissions` na claim, nao faz nada.
    2. Se eh super admin, nao precisa de lista (has_permission faz bypass).
    3. Caso contrario, busca em `POST /auth/me/full` (cache TTL 60s) e
       popula in-place: `permissions` (uniao flat), `permission_detail`
       (bloco estruturado p/ escopo por produto) e promove
       `is_super_admin` se o AdminCenter afirmar. Em caso de falha de
       rede, deixa como esta (None) — caller decide fail-open/closed.
    """
    if user.permissions is not None:
        return user
    if user.is_super_admin:
        return user
    detail = _cached_permissions(user.user_id, token)
    if detail is not None:
        user.permissions = detail["merged"]
        user.permission_detail = {
            "organization_permissions": detail["organization_permissions"],
            "products": detail["products"],
        }
        if detail["is_super_admin"]:
            user.is_super_admin = True
    return user


def _require_permissions_dependency(permissions: List[str], product_slug: Optional[str]):
    """Base comum de require_permission / require_any_permission.

    Fail-closed: se as permissoes nao puderem ser resolvidas (AdminCenter
    indisponivel), NEGA com 503 — a menos que AUTH_PRODUCT_GATE_FAIL_OPEN=true.
    """
    async def _dependency(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> AuthenticatedUser:
        auth = _get_auth()
        token = credentials.credentials

        user = auth.validate_token(token)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token invalido ou expirado",
                headers={"WWW-Authenticate": "Bearer"},
            )

        enrich_user_with_permissions(user, token)

        if not user.is_super_admin and user.permissions is None:
            # Nao foi possivel resolver permissoes (AdminCenter indisponivel).
            logger.warning(
                "Permissoes de '%s' nao resolvidas (AdminCenter indisponivel).",
                user.email,
            )
            if not _gate_fail_open():
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Validacao de acesso indisponivel. Tente novamente.",
                )
            logger.warning(
                "AUTH_PRODUCT_GATE_FAIL_OPEN=true — liberando sem checar "
                "permissoes de '%s'.", user.email,
            )
            return user

        if not has_any_permission(user, permissions, product_slug):
            if len(permissions) == 1:
                detail_msg = f"Permissao '{permissions[0]}' necessaria"
            else:
                detail_msg = f"Uma das permissoes necessaria: {', '.join(permissions)}"
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=detail_msg,
            )
        return user

    return _dependency


def require_permission(permission: str, product_slug: Optional[str] = None):
    """FastAPI Dependency Factory - exige uma permissao especifica.

    Faz enriquecimento via `/auth/me/full` se necessario, depois aplica
    `has_permission`. Levanta 403 quando o user nao tem; 503 quando as
    permissoes nao podem ser resolvidas (fail-closed, ver
    AUTH_PRODUCT_GATE_FAIL_OPEN).

    Uso:
        @app.post("/dashboards")
        async def criar(user = Depends(require_permission("dashboards:manage"))):
            ...
    """
    return _require_permissions_dependency([permission], product_slug)


def require_any_permission(permissions: List[str], product_slug: Optional[str] = None):
    """Variante OR: passa se o user tiver pelo menos uma das permissoes."""
    return _require_permissions_dependency(list(permissions), product_slug)
