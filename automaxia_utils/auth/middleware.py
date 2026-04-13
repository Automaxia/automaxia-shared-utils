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
        """
        if not self.config.admincenter_url:
            logger.error("ADMIN_CENTER_URL nao configurada para validacao remota")
            return None

        url = f"{self.config.admincenter_url}/auth/validate-product-access"

        try:
            session = self._get_session()
            response = session.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"product_slug": self.config.product_slug} if self.config.product_slug else {},
                timeout=10,
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    user_data = data.get("data", {})
                    user = AuthenticatedUser(
                        user_id=user_data.get("user_id", ""),
                        email=user_data.get("email", ""),
                        organization_id=user_data.get("organization_id"),
                        first_name=user_data.get("first_name"),
                        last_name=user_data.get("last_name"),
                        status=user_data.get("status", "active"),
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

            if response.status_code == 401:
                logger.info("Token rejeitado pelo AdminCenter (401)")
            elif response.status_code == 403:
                logger.warning("Acesso negado ao produto pelo AdminCenter (403)")
            else:
                logger.warning(f"AdminCenter retornou {response.status_code}")

            return None

        except requests.RequestException as e:
            logger.error(f"Erro ao validar token no AdminCenter: {e}")
            return None

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
        """Converte payload JWT em AuthenticatedUser."""
        return AuthenticatedUser(
            user_id=payload.get("user_id", ""),
            email=payload.get("sub", ""),
            organization_id=payload.get("organization_id"),
            status="active",
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
            try:
                session = auth._get_session()
                response = session.post(
                    f"{auth.config.admincenter_url}/auth/validate-product-access",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"product_slug": slug},
                    timeout=10,
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("success") and "product_access" in data.get("data", {}):
                        pa = data["data"]["product_access"]
                        user.product_access = ProductAccess(
                            product_id=pa.get("product_id"),
                            product_slug=pa.get("product_slug"),
                            profile_name=pa.get("profile_name"),
                            permissions=pa.get("permissions", {}),
                            is_active=pa.get("is_active", True),
                        )
                elif response.status_code == 403:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Acesso negado ao produto '{slug}'",
                    )
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"Erro ao verificar acesso ao produto: {e}")
                # Em caso de falha na rede, permite acesso (graceful degradation)

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
