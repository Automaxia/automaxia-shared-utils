import os
import sys
from pathlib import Path

# Adicionar src ao path
sys.path.append(str(Path(__file__).parent.parent))

from automaxia_utils.admin_center.service import AdminCenterConfig

def test_config_logic():
    print("🧪 Testando lógica de configuração de ambiente...\n")
    
    # Mocking environment variables
    os.environ["ADMIN_CENTER_URL"] = "https://prod-api.example.com"
    os.environ["ADMIN_CENTER_DEV_URL"] = "http://dev-ip:8000/api"
    os.environ["ADMIN_CENTER_API_KEY"] = "test-key"
    os.environ["ADMIN_CENTER_PRODUCT_ID"] = "prod-id"
    os.environ["ADMIN_CENTER_ENVIRONMENT_ID"] = "env-prod-uuid"
    os.environ["ADMIN_CENTER_ENVIRONMENT_ID_DEV"] = "env-dev-uuid"
    
    # Caso 1: Produção
    os.environ["ENVIRONMENT"] = "production"
    config_prod = AdminCenterConfig.from_env()
    print(f"CASE 1 (Production):")
    print(f"  URL: {config_prod.api_url}")
    assert config_prod.api_url == "https://prod-api.example.com"
    print("  ✅ URL de produção correta!")
    
    # Caso 2: Desenvolvimento
    os.environ["ENVIRONMENT"] = "development"
    config_dev = AdminCenterConfig.from_env()
    print(f"\nCASE 2 (Development):")
    print(f"  URL: {config_dev.api_url}")
    assert config_dev.api_url == "http://dev-ip:8000/api"
    print("  ✅ URL de desenvolvimento correta (via ADMIN_CENTER_DEV_URL)!")
    
    # Caso 3: Fallback para LOCAL
    del os.environ["ADMIN_CENTER_DEV_URL"]
    os.environ["ADMIN_CENTER_URL_LOCAL"] = "http://127.0.0.1:8000/api"
    config_local = AdminCenterConfig.from_env()
    print(f"\nCASE 3 (Fallback Local):")
    print(f"  URL: {config_local.api_url}")
    assert config_local.api_url == "http://127.0.0.1:8000/api"
    print("  ✅ URL de desenvolvimento correta (via fallback ADMIN_CENTER_URL_LOCAL)!")

    print("\n🎉 Todos os testes de configuração passaram!")

if __name__ == "__main__":
    test_config_logic()
