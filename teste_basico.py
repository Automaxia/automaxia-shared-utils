"""Teste basico da lib automaxia-shared-utils.

Verifica que cada modulo principal carrega e expoe os simbolos esperados,
sem depender de OpenAI/AdminCenter remoto. Use:

    python teste_basico.py
"""
import sys


def _check(label: str, fn):
    try:
        fn()
        print(f"  [OK]   {label}")
        return True
    except Exception as e:
        print(f"  [FAIL] {label}: {type(e).__name__}: {e}")
        return False


def main() -> int:
    failures = 0

    # 1. Versao
    print("[1/6] Versao do pacote")
    import automaxia_utils
    print(f"  __version__ = {automaxia_utils.__version__}")
    if automaxia_utils.__version__ < "1.4.0":
        print("  AVISO: versao antiga, esperado >= 1.4.0")

    # 2. AdminCenter (sem inicializar — sem rede)
    print("\n[2/6] AdminCenter Service")
    failures += not _check("import AdminCenterService", lambda: __import__(
        'automaxia_utils', fromlist=['AdminCenterService']).AdminCenterService)
    failures += not _check("import AdminCenterConfig", lambda: __import__(
        'automaxia_utils', fromlist=['AdminCenterConfig']).AdminCenterConfig)
    failures += not _check("import get_admin_center_service", lambda: __import__(
        'automaxia_utils', fromlist=['get_admin_center_service']).get_admin_center_service)

    def _config_disabled():
        from automaxia_utils import AdminCenterConfig
        cfg = AdminCenterConfig(enabled=False)
        assert cfg.is_valid(), "AdminCenterConfig(enabled=False) deveria ser valido"
    failures += not _check("AdminCenterConfig(enabled=False).is_valid()", _config_disabled)

    # 3. Resolucao de modo (test|live)
    print("\n[3/6] Resolucao de modo (test|live)")
    def _mode_from_test_key():
        from automaxia_utils import AdminCenterConfig, AdminCenterService
        svc = AdminCenterService.__new__(AdminCenterService)
        svc.config = AdminCenterConfig(api_key="sk_test_abc123", enabled=False)
        assert svc._resolve_mode_header() == "test", "deveria inferir 'test' do prefixo"
    failures += not _check("infere mode='test' de sk_test_*", _mode_from_test_key)

    def _mode_from_live_key():
        from automaxia_utils import AdminCenterConfig, AdminCenterService
        svc = AdminCenterService.__new__(AdminCenterService)
        svc.config = AdminCenterConfig(api_key="sk_live_xyz789", enabled=False)
        assert svc._resolve_mode_header() == "live", "deveria inferir 'live' do prefixo"
    failures += not _check("infere mode='live' de sk_live_*", _mode_from_live_key)

    # 4. JobRunner (sem subir scheduler — apenas instanciar)
    print("\n[4/6] JobRunner")
    failures += not _check("import JobRunner", lambda: __import__(
        'automaxia_utils', fromlist=['JobRunner']).JobRunner)

    def _instantiate_runner():
        from automaxia_utils import JobRunner, AdminCenterConfig, AdminCenterService
        svc = AdminCenterService(AdminCenterConfig(enabled=False))
        runner = JobRunner(svc)
        runner.register("rpa_boletos.rodada", lambda: None)
        assert "rpa_boletos.rodada" in runner._handlers
    failures += not _check("instancia JobRunner + register", _instantiate_runner)

    # 5. Modulo de jobs (deps opcionais — so necessarias quando o produto
    # realmente usar JobRunner.start(). Import lazy em jobs.py.)
    print("\n[5/6] Dependencias de jobs (apscheduler, croniter) — opcional")
    def _check_optional(label, fn):
        try:
            fn()
            print(f"  [OK]   {label}")
        except ImportError as e:
            print(f"  [WARN] {label}: {e}")
            print(f"         Resolva com: pip install -r requirements.txt")
    _check_optional("apscheduler importavel", lambda: __import__('apscheduler.schedulers.background'))
    _check_optional("croniter importavel", lambda: __import__('croniter'))

    # 6. Token tracking (sem chamar OpenAI — apenas estimativa local)
    print("\n[6/6] Token tracking (estimativa local)")
    def _estimate():
        from automaxia_utils import estimate_tokens_and_cost
        result = estimate_tokens_and_cost(
            prompt="Ola mundo, este e um teste basico da lib automaxia-utils.",
            model="gpt-3.5-turbo"
        )
        total = result.get("estimated_total_tokens") or result.get("total_tokens") or 0
        assert total > 0, f"estimativa deveria ser > 0, recebeu {total}: {result}"
        cost = result.get("estimated_cost_usd") or result.get("cost_usd") or 0
        print(f"     -> tokens={total}, custo=${cost:.6f}, modelo={result.get('model')}")
    failures += not _check("estimate_tokens_and_cost", _estimate)

    # Resumo
    print()
    if failures == 0:
        print("Tudo OK. Lib v" + automaxia_utils.__version__ + " carregada e funcional.")
        return 0
    print(f"FALHOU: {failures} teste(s) com erro.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
