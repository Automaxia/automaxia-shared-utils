"""
Testes unitários para o middleware de autenticação (automaxia_utils/auth/middleware.py).
Cobre AdminCenterAuthConfig, AdminCenterAuth e validação local de JWT.
"""
import os
import time
import pytest
from datetime import datetime, timedelta, timezone

os.environ["SECRET_KEY"] = "test-secret-key-minimum-32-chars-for-jwt-security"
os.environ["ADMIN_CENTER_URL"] = "http://fake-admin-center:8000/api"
os.environ["PRODUCT_SLUG"] = "dashboard"
os.environ["AUTH_LOCAL_VALIDATION"] = "true"

from automaxia_utils.auth.middleware import (
    AdminCenterAuthConfig,
    AdminCenterAuth,
    AuthenticatedUser,
    ProductAccess,
)


# ── AdminCenterAuthConfig ────────────────────────────────────────────────

class TestAuthConfig:
    def test_from_env_carrega_secret_key(self):
        config = AdminCenterAuthConfig.from_env()
        assert config.secret_key == "test-secret-key-minimum-32-chars-for-jwt-security"

    def test_from_env_carrega_url(self):
        config = AdminCenterAuthConfig.from_env()
        assert config.admincenter_url != ""  # carregou alguma URL do env

    def test_from_env_carrega_product_slug(self):
        config = AdminCenterAuthConfig.from_env()
        assert config.product_slug == "dashboard"

    def test_from_env_local_validation_true(self):
        config = AdminCenterAuthConfig.from_env()
        assert config.local_validation is True

    def test_defaults(self):
        config = AdminCenterAuthConfig()
        assert config.algorithm == "HS256"
        assert config.cache_ttl == 300
        assert config.local_validation is True


# ── AuthenticatedUser Model ──────────────────────────────────────────────

class TestAuthenticatedUser:
    def test_cria_usuario_basico(self):
        user = AuthenticatedUser(
            user_id="usr-123",
            email="test@linedata.com.br",
        )
        assert user.user_id == "usr-123"
        assert user.email == "test@linedata.com.br"
        assert user.status == "active"

    def test_cria_usuario_completo(self):
        access = ProductAccess(
            product_id="prod-1",
            product_slug="dashboard",
            profile_name="admin",
            permissions={"read": True, "write": True},
        )
        user = AuthenticatedUser(
            user_id="usr-456",
            email="admin@test.com",
            organization_id="org-789",
            first_name="Admin",
            last_name="Test",
            product_access=access,
        )
        assert user.organization_id == "org-789"
        assert user.product_access.profile_name == "admin"

    def test_product_access_optional(self):
        user = AuthenticatedUser(user_id="usr-1", email="a@b.com")
        assert user.product_access is None


# ── ProductAccess Model ──────────────────────────────────────────────────

class TestProductAccess:
    def test_cria_product_access(self):
        pa = ProductAccess(
            product_id="prod-1",
            product_slug="datachatai",
            profile_name="operator",
            permissions={"query": True},
            is_active=True,
        )
        assert pa.product_slug == "datachatai"
        assert pa.is_active is True

    def test_defaults(self):
        pa = ProductAccess()
        assert pa.product_id is None
        assert pa.is_active is True
        assert pa.permissions == {}


# ── AdminCenterAuth - Validação Local ────────────────────────────────────

class TestAdminCenterAuthLocal:
    @pytest.fixture
    def auth(self):
        config = AdminCenterAuthConfig(
            secret_key="test-secret-key-minimum-32-chars-for-jwt-security",
            algorithm="HS256",
            local_validation=True,
        )
        return AdminCenterAuth(config)

    def _create_token(self, payload, secret="test-secret-key-minimum-32-chars-for-jwt-security"):
        """Helper para criar JWT de teste."""
        try:
            from jose import jwt
            return jwt.encode(payload, secret, algorithm="HS256")
        except ImportError:
            import jwt as pyjwt
            return pyjwt.encode(payload, secret, algorithm="HS256")

    def test_valida_token_local_valido(self, auth):
        payload = {
            "sub": "admin@linedata.com.br",
            "user_id": "usr-123",
            "organization_id": "org-456",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "iat": datetime.now(timezone.utc),
        }
        token = self._create_token(payload)
        user = auth.validate_token_local(token)
        assert user is not None
        assert user.email == "admin@linedata.com.br"

    def test_rejeita_token_expirado(self, auth):
        payload = {
            "sub": "test@test.com",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        }
        token = self._create_token(payload)
        user = auth.validate_token_local(token)
        assert user is None

    def test_rejeita_token_com_secret_errada(self, auth):
        payload = {
            "sub": "test@test.com",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        token = self._create_token(payload, secret="wrong-secret-key-wrong-secret-key!")
        user = auth.validate_token_local(token)
        assert user is None

    def test_rejeita_token_invalido(self, auth):
        user = auth.validate_token_local("not-a-valid-jwt-token")
        assert user is None


# ── Cache de Usuário ─────────────────────────────────────────────────────

class TestAuthCache:
    def test_cache_armazena_usuario(self):
        config = AdminCenterAuthConfig(
            secret_key="test-key-32-chars-min-for-tests!!",
            cache_ttl=300,
        )
        auth_service = AdminCenterAuth(config)
        user = AuthenticatedUser(user_id="usr-1", email="a@b.com")
        auth_service._cache_user("key1", user)
        cached = auth_service._get_cached_user("key1")
        assert cached is not None
        assert cached.email == "a@b.com"

    def test_cache_retorna_none_para_chave_inexistente(self):
        config = AdminCenterAuthConfig(secret_key="test-key-32-chars-min-for-tests!!")
        auth_service = AdminCenterAuth(config)
        assert auth_service._get_cached_user("nao_existe") is None

    def test_cache_expira(self):
        config = AdminCenterAuthConfig(
            secret_key="test-key-32-chars-min-for-tests!!",
            cache_ttl=1,  # 1 segundo
        )
        auth_service = AdminCenterAuth(config)
        user = AuthenticatedUser(user_id="usr-1", email="a@b.com")
        auth_service._cache_user("key_exp", user)
        time.sleep(1.5)
        assert auth_service._get_cached_user("key_exp") is None

    def test_invalidar_cache(self):
        config = AdminCenterAuthConfig(secret_key="test-key-32-chars-min-for-tests!!")
        auth_service = AdminCenterAuth(config)
        user = AuthenticatedUser(user_id="usr-1", email="a@b.com")
        auth_service._cache_user("key_inv", user)
        auth_service.invalidate_cache()
        assert auth_service._get_cached_user("key_inv") is None
