"""Auto-registro de produtos satelites no AdminCenter.

Cada produto (dashboard, talk, ...) usa este modulo no boot para:

1. Enviar seu manifesto (`code`, `version`, `base_url`, `permissions[]`,
   `menus[]`) para o AdminCenter via `POST /product/register`.
2. Iniciar um loop de heartbeat (`POST /product/{code}/heartbeat`) em
   thread daemon, indicando que esta vivo.

Auth: header `x-product-key` (shared secret). Defina a mesma chave neste
produto (env `PRODUCT_REGISTRATION_KEY`) e no AdminCenter.

Uso minimo no FastAPI:

    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from automaxia_utils.registration import (
        ProductManifest, register_with_platform, start_heartbeat_loop,
    )

    MANIFEST = ProductManifest(
        code='dashboard',
        name='Automaxia Dashboard',
        version='1.4.2',
        organization_slug='automaxia',  # obrigatorio na PRIMEIRA criacao
        permissions=[{'key': 'dashboards:read', 'label': 'Ver dashboards'}],
        menus=[{'key': 'dashboards-gerador', 'label': 'Dashboards', 'route': '/admin/dashboards'}],
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        register_with_platform(MANIFEST)
        stop = start_heartbeat_loop(MANIFEST.code, auto_reregister_manifest=MANIFEST)
        yield
        stop()

    app = FastAPI(lifespan=lifespan)

Robustez: nem `register_with_platform` nem o heartbeat-loop devem matar
o produto se o AdminCenter estiver indisponivel — eles logam erro e
seguem. O produto fica disponivel para servir trafego mesmo sem
catalogo atualizado.
"""
from .client import (
    ProductManifest,
    ProductRegistrationConfig,
    register_with_platform,
    send_heartbeat,
    start_heartbeat_loop,
)

__all__ = [
    "ProductManifest",
    "ProductRegistrationConfig",
    "register_with_platform",
    "send_heartbeat",
    "start_heartbeat_loop",
]
