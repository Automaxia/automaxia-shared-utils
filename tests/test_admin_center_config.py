"""
Testes unitários para AdminCenterConfig e AdminCenterService.
Cobre configuração, singleton, e lifecycle do service.
"""
import os
import pytest

from automaxia_utils.admin_center.service import (
    AdminCenterConfig,
    AdminCenterService,
    get_admin_center_service,
    reset_admin_center_service,
)


# ── AdminCenterConfig ────────────────────────────────────────────────────

class TestAdminCenterConfig:
    def test_from_env_carrega_campos(self):
        os.environ["ADMIN_CENTER_URL"] = "https://test-api.example.com"
        os.environ["ADMIN_CENTER_API_KEY"] = "test-api-key"
        os.environ["ADMIN_CENTER_PRODUCT_ID"] = "prod-123"
        os.environ["ADMIN_CENTER_ENVIRONMENT_ID"] = "env-456"
        os.environ["ADMIN_CENTER_ORGANIZATION_ID"] = "org-789"
        os.environ["ENVIRONMENT"] = "production"
        config = AdminCenterConfig.from_env()
        assert config.api_key == "test-api-key"
        assert config.product_id == "prod-123"
        assert config.environment_id == "env-456"
        assert config.organization_id == "org-789"

    def test_is_valid_com_campos_obrigatorios(self):
        config = AdminCenterConfig(
            api_url="http://test",
            api_key="key",
            product_id="prod",
            environment_id="env",
            organization_id="org",
        )
        assert config.is_valid() is True

    def test_is_valid_sem_api_url(self):
        config = AdminCenterConfig(
            api_url="",
            api_key="key",
            product_id="prod",
            environment_id="env",
            organization_id="org",
        )
        assert config.is_valid() is False

    def test_is_valid_sem_api_key(self):
        config = AdminCenterConfig(
            api_url="http://test",
            api_key="",
            product_id="prod",
            environment_id="env",
            organization_id="org",
        )
        assert config.is_valid() is False

    def test_valores_padrao(self):
        config = AdminCenterConfig(
            api_url="http://test",
            api_key="key",
            product_id="prod",
            environment_id="env",
            organization_id="org",
        )
        assert config.batch_size == 50
        assert config.batch_interval == 2
        assert config.timeout == 10
        assert config.max_retries == 2
        assert config.queue_max_size == 1000


# ── Singleton ────────────────────────────────────────────────────────────

class TestSingleton:
    def test_reset_service(self):
        reset_admin_center_service()

    def test_get_service_retorna_instancia(self):
        reset_admin_center_service()
        config = AdminCenterConfig(
            api_url="http://fake",
            api_key="fake-key",
            product_id="prod",
            environment_id="env",
            organization_id="org",
            enabled=False,
        )
        service = get_admin_center_service(config)
        assert service is not None
        assert isinstance(service, AdminCenterService)
        reset_admin_center_service()

    def test_get_service_retorna_mesma_instancia(self):
        reset_admin_center_service()
        config = AdminCenterConfig(
            api_url="http://fake",
            api_key="fake-key",
            product_id="prod",
            environment_id="env",
            organization_id="org",
            enabled=False,
        )
        s1 = get_admin_center_service(config)
        s2 = get_admin_center_service(config)
        assert s1 is s2
        reset_admin_center_service()
