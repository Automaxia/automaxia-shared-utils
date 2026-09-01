"""
Testes unitários para o middleware de autenticação (automaxia_utils/auth/middleware.py).
Cobre AdminCenterAuthConfig, AdminCenterAuth e validação local de JWT.
"""
import os
import time
import pytest
from datetime import datetime, timedelta, timezone

# ⚠️ Semear `os.environ` no nivel do MODULO vale para as chaves abaixo, que
# nenhum outro modulo de teste toca. NAO vale para o slug de produto: o
# `test_sync_balance_1_14.py` escreve `ADMIN_CENTER_PRODUCT_SLUG` em
# `os.environ`, e como `from_env` da precedencia a ele, o valor semeado aqui
# perdia — dependendo apenas da ORDEM em que o pytest importa os modulos.
# Um teste que muda de resultado conforme o vizinho e' pior que teste ausente.
# Por isso os testes de slug usam `monkeypatch`, que isola e restaura.
os.environ["SECRET_KEY"] = "test-secret-key-minimum-32-chars-for-jwt-security"
os.environ["ADMIN_CENTER_URL"] = "http://fake-admin-center:8000/api"
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

    def test_from_env_prefere_admin_center_product_slug(self, monkeypatch):
        """`ADMIN_CENTER_PRODUCT_SLUG` vence `PRODUCT_SLUG`.

        E o que os satelites do Studio definem; lendo so' `PRODUCT_SLUG`, o
        gate de produto era pulado em silencio.
        """
        monkeypatch.setenv("ADMIN_CENTER_PRODUCT_SLUG", "talk")
        monkeypatch.setenv("PRODUCT_SLUG", "vision")
        assert AdminCenterAuthConfig.from_env().product_slug == "talk"

    def test_from_env_cai_em_product_slug_quando_o_novo_falta(self, monkeypatch):
        """Retrocompatibilidade: quem so define o nome antigo continua valendo."""
        monkeypatch.delenv("ADMIN_CENTER_PRODUCT_SLUG", raising=False)
        monkeypatch.setenv("PRODUCT_SLUG", "vision")
        assert AdminCenterAuthConfig.from_env().product_slug == "vision"

    def test_from_env_sem_nenhum_dos_dois_devolve_vazio(self, monkeypatch):
        monkeypatch.delenv("ADMIN_CENTER_PRODUCT_SLUG", raising=False)
        monkeypatch.delenv("PRODUCT_SLUG", raising=False)
        assert AdminCenterAuthConfig.from_env().product_slug == ""

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
            email="test@automaxia.com.br",
        )
        assert user.user_id == "usr-123"
        assert user.email == "test@automaxia.com.br"
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
            product_slug="talk",
            profile_name="operator",
            permissions={"query": True},
            is_active=True,
        )
        assert pa.product_slug == "talk"
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
            "sub": "admin@automaxia.com.br",
            "user_id": "usr-123",
            "organization_id": "org-456",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "iat": datetime.now(timezone.utc),
        }
        token = self._create_token(payload)
        user = auth.validate_token_local(token)
        assert user is not None
        assert user.email == "admin@automaxia.com.br"

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
