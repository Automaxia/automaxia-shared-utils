"""
Testes unitários dos helpers RBAC (automaxia_utils/auth/middleware.py, v1.11).
Cobre has_permission/has_any_permission (escopo org vs produto, super admin),
_fetch_permissions_from_me (mock de requests) e enrich_user_with_permissions.
"""
import os
import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-minimum-32-chars-for-jwt-security")
os.environ.setdefault("ADMIN_CENTER_URL", "http://fake-admin-center:8000/api")

from automaxia_utils.auth import middleware
from automaxia_utils.auth.middleware import (
    AdminCenterAuthConfig,
    AuthenticatedUser,
    has_permission,
    has_any_permission,
    enrich_user_with_permissions,
    invalidate_permission_cache,
)


def _user(**kwargs):
    base = {"user_id": "usr-1", "email": "a@b.com"}
    base.update(kwargs)
    return AuthenticatedUser(**base)


# ── has_permission ───────────────────────────────────────────────────────

class TestHasPermission:
    def test_super_admin_bypassa_tudo(self):
        user = _user(is_super_admin=True, permissions=None)
        assert has_permission(user, "qualquer:coisa") is True
        assert has_permission(user, "qualquer:coisa", product_slug="dashboard") is True

    def test_user_none_nega(self):
        assert has_permission(None, "users:read") is False

    def test_permissions_nao_resolvidas_nega(self):
        user = _user(permissions=None)
        assert has_permission(user, "users:read") is False

    def test_lista_flat_do_jwt(self):
        user = _user(permissions=["users:read", "dashboards:manage"])
        assert has_permission(user, "users:read") is True
        assert has_permission(user, "users:manage") is False

    def test_org_permission_vale_para_qualquer_produto(self):
        user = _user(
            permissions=["users:manage"],
            permission_detail={
                "organization_permissions": ["users:manage"],
                "products": {},
            },
        )
        assert has_permission(user, "users:manage") is True
        assert has_permission(user, "users:manage", product_slug="dashboard") is True
        assert has_permission(user, "users:manage", product_slug="talk") is True

    def test_product_permission_escopada_por_slug(self):
        user = _user(
            permissions=["dashboards:read"],
            permission_detail={
                "organization_permissions": [],
                "products": {"dashboard": ["dashboards:read"]},
            },
        )
        assert has_permission(user, "dashboards:read", product_slug="dashboard") is True
        assert has_permission(user, "dashboards:read", product_slug="talk") is False
        # Sem product_slug, qualquer produto vale
        assert has_permission(user, "dashboards:read") is True

    def test_has_any_permission(self):
        user = _user(permissions=["a:read"])
        assert has_any_permission(user, ["a:read", "b:read"]) is True
        assert has_any_permission(user, ["b:read", "c:read"]) is False
        assert has_any_permission(user, []) is False
        assert has_any_permission(_user(is_super_admin=True), ["x:y"]) is True


# ── _fetch_permissions_from_me / enrich (mock de requests) ──────────────

class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(url)
        return self.response


ME_FULL_BODY = {
    "success": True,
    "data": {
        "user_id": "usr-1",
        "email": "a@b.com",
        "is_super_admin": False,
        "organization_permissions": ["users:manage"],
        "products": [
            {
                "product_id": "p1",
                "product_slug": "dashboard",
                "product_name": "Dashboard",
                "permissions": ["dashboards:read", "dashboards:manage"],
            },
        ],
        "accessible_products": ["dashboard"],
    },
    "message": "",
    "status_code": 200,
}


@pytest.fixture
def fake_auth():
    """Configura singleton de auth com session fake e limpa caches."""
    def _install(response):
        config = AdminCenterAuthConfig(
            admincenter_url="http://fake-admin-center:8000/api",
            secret_key="test-secret-key-minimum-32-chars-for-jwt-security",
        )
        middleware.configure_auth(config)
        session = FakeSession(response)
        middleware._auth_instance._session = session
        return session

    invalidate_permission_cache()
    yield _install
    invalidate_permission_cache()
    middleware._auth_instance = None


class TestFetchPermissions:
    def test_fetch_ok_estrutura(self, fake_auth):
        session = fake_auth(FakeResponse(200, ME_FULL_BODY))
        detail = middleware._fetch_permissions_from_me("tok")
        assert session.calls == ["http://fake-admin-center:8000/api/auth/me/full"]
        assert detail["organization_permissions"] == ["users:manage"]
        assert detail["products"] == {
            "dashboard": ["dashboards:read", "dashboards:manage"]
        }
        assert detail["merged"] == [
            "users:manage", "dashboards:read", "dashboards:manage"
        ]
        assert detail["is_super_admin"] is False

    def test_fetch_envelope_success_false(self, fake_auth):
        fake_auth(FakeResponse(200, {"success": False, "status_code": 500}))
        assert middleware._fetch_permissions_from_me("tok") is None

    def test_fetch_status_5xx(self, fake_auth):
        fake_auth(FakeResponse(503, {}, text="down"))
        assert middleware._fetch_permissions_from_me("tok") is None


class TestEnrichUser:
    def test_enrich_popula_permissions_e_detail(self, fake_auth):
        fake_auth(FakeResponse(200, ME_FULL_BODY))
        user = _user(permissions=None)
        enrich_user_with_permissions(user, "tok")
        assert user.permissions == [
            "users:manage", "dashboards:read", "dashboards:manage"
        ]
        assert has_permission(user, "dashboards:read", product_slug="dashboard")
        assert not has_permission(user, "dashboards:read", product_slug="outro")
        assert has_permission(user, "users:manage", product_slug="outro")

    def test_enrich_usa_cache_ttl(self, fake_auth):
        session = fake_auth(FakeResponse(200, ME_FULL_BODY))
        enrich_user_with_permissions(_user(), "tok")
        enrich_user_with_permissions(_user(), "tok")
        assert len(session.calls) == 1  # segunda chamada veio do cache

    def test_enrich_nao_sobrescreve_claim_do_jwt(self, fake_auth):
        session = fake_auth(FakeResponse(200, ME_FULL_BODY))
        user = _user(permissions=["x:read"])
        enrich_user_with_permissions(user, "tok")
        assert user.permissions == ["x:read"]
        assert session.calls == []  # nao houve roundtrip

    def test_enrich_falha_deixa_none(self, fake_auth):
        fake_auth(FakeResponse(503, {}, text="down"))
        user = _user(permissions=None)
        enrich_user_with_permissions(user, "tok")
        assert user.permissions is None  # caller decide fail-open/closed

    def test_enrich_promove_super_admin(self, fake_auth):
        body = {
            "success": True,
            "data": {**ME_FULL_BODY["data"], "is_super_admin": True},
        }
        fake_auth(FakeResponse(200, body))
        user = _user(permissions=None)
        enrich_user_with_permissions(user, "tok")
        assert user.is_super_admin is True
