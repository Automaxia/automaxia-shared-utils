"""Cliente de auto-registro do produto satelite no AdminCenter.

Implementa a parte cliente do contrato definido no admincenter-api
(endpoints `POST /product/register` e `POST /product/{code}/heartbeat`).

Filosofia
---------
- **Best-effort**: o AdminCenter pode estar fora do ar quando o produto
  sobe. Erros sao logados, nunca propagados — o produto deve subir mesmo
  sem catalogo atualizado.
- **Sync com thread daemon**: o heartbeat roda em thread daemon, fora do
  event loop do FastAPI. Isso evita acoplamento com asyncio e mantem o
  modulo usavel em qualquer framework (Flask, Django, jobs Celery, etc.).
- **Sem dependencias novas**: usa apenas `requests` (ja dep do package) e
  stdlib. Sem PyYAML — manifesto e declarado em Python.
- **Envelope do AdminCenter**: respostas vem em
  `{success, data, message, status_code}` e o backend legado pode devolver
  HTTP 200 com `success: false` — o cliente trata os dois casos.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import requests


logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Tipos de dado
# ----------------------------------------------------------------------


@dataclass
class ProductManifest:
    """Manifesto declarado pelo produto.

    Os campos correspondem 1:1 ao schema Pydantic `ProductManifest` no
    admincenter-api. Mudancas de contrato seguem via bump de
    `contract_version`.
    """
    code: str
    name: str
    version: str

    base_url: Optional[str] = None
    health_path: str = "/health"
    contract_version: str = "1.0"

    description: Optional[str] = None
    product_type: Optional[str] = None
    frontend_url: Optional[str] = None
    documentation_url: Optional[str] = None
    repository_url: Optional[str] = None

    # None = "nao declarado": o AdminCenter mantem o valor atual do catalogo.
    requires_connection: Optional[bool] = None
    # IMPORTANTE: manter default None DE PROPOSITO. Com default False, o
    # re-registro disparado pelo heartbeat (404 -> auto_reregister_manifest)
    # zerava a flag `requires_instance` ajustada manualmente na UI da
    # plataforma — bug conhecido do Cockpit. None = "nao mexer".
    requires_instance: Optional[bool] = None
    # Engines de banco que o produto sabe consumir (ex.: ['postgresql','mysql']).
    # Filtra o seletor de conexao mostrado ao usuario. Mesma convencao dos
    # demais: None = "nao declarado", o AdminCenter mantem o que ja esta la.
    # Adicionado em 17/08/2026 (1.12.0): os manifests do dashai e do turing ja
    # declaravam este campo — o dataclass nao aceitava, e o `except Exception`
    # do manifest do turing engolia o TypeError e deixava PRODUCT_MANIFEST=None,
    # ou seja, o produto simplesmente nunca se registrava, em silencio.
    requires_connection_engines: Optional[List[str]] = None

    # Silo do AdminCenter em que o produto se registra ('test' | 'live').
    # Nao existe no Cockpit.
    mode: str = "live"
    # Obrigatorio na PRIMEIRA criacao do produto (define a org dona);
    # ignorado pelo AdminCenter nos registros seguintes. Nao existe no Cockpit.
    organization_slug: Optional[str] = None

    # Cada permissao: {'key': 'recurso:acao', 'label': str, 'description'?: str, 'category'?: str}
    permissions: List[Dict[str, Any]] = field(default_factory=list)
    # Cada menu: {'key': str, 'label': str, 'icon'?: str, 'route'?: str, 'requires'?: str, 'order'?: int}
    menus: List[Dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        """Converte para o payload JSON aceito por POST /product/register.

        Remove chaves com valor None para nao sobrescrever no catalogo
        valores que o produto nao declarou (vide `requires_instance`) nem
        poluir o JSONB guardado na plataforma com chaves nulas.
        """
        data = asdict(self)
        return {key: value for key, value in data.items() if value is not None}


@dataclass
class ProductRegistrationConfig:
    """Config do cliente de registro.

    Defaults sao puxados do ambiente — pratico para produtos que ja
    seguem `from_env`-style config. Pode ser sobrescrito explicitamente.
    """
    platform_url: str = ""
    product_key: str = ""
    timeout_seconds: int = 10
    heartbeat_interval_seconds: int = 60
    register_max_retries: int = 3
    register_retry_backoff_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> "ProductRegistrationConfig":
        # ADMIN_CENTER_URL e o nome consolidado nos produtos consumidores.
        # Em ENVIRONMENT=development, prioriza ADMIN_CENTER_DEV_URL /
        # ADMIN_CENTER_URL_LOCAL para nao mandar o register/heartbeat para
        # a URL de producao quando o produto roda local apontando para um
        # AdminCenter dev.
        environment = os.getenv("ENVIRONMENT", "production").lower()
        platform_url = (os.getenv("ADMIN_CENTER_URL") or "").strip()
        if environment == "development":
            dev_url = (
                os.getenv("ADMIN_CENTER_DEV_URL")
                or os.getenv("ADMIN_CENTER_URL_LOCAL")
                or ""
            ).strip()
            if dev_url:
                platform_url = dev_url
        if platform_url.endswith("/"):
            platform_url = platform_url[:-1]
        if platform_url and not platform_url.endswith("/api"):
            platform_url = f"{platform_url}/api"
        return cls(
            platform_url=platform_url,
            product_key=os.getenv("PRODUCT_REGISTRATION_KEY", "").strip(),
            timeout_seconds=int(os.getenv("PRODUCT_REGISTRATION_TIMEOUT", "10")),
            heartbeat_interval_seconds=int(
                os.getenv("PRODUCT_HEARTBEAT_INTERVAL_SECONDS", "60")
            ),
            register_max_retries=int(os.getenv("PRODUCT_REGISTRATION_MAX_RETRIES", "3")),
            register_retry_backoff_seconds=float(
                os.getenv("PRODUCT_REGISTRATION_BACKOFF_SECONDS", "2.0")
            ),
        )

    def is_usable(self) -> bool:
        return bool(self.platform_url)


# ----------------------------------------------------------------------
# Operacoes
# ----------------------------------------------------------------------


def _headers(config: ProductRegistrationConfig) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config.product_key:
        headers["x-product-key"] = config.product_key
    return headers


def _envelope_ok(resp: requests.Response) -> bool:
    """True se a resposta representa sucesso, considerando o envelope.

    O backend legado do AdminCenter as vezes devolve HTTP 200 com body
    `{success: false, status_code: ...}` — isso conta como falha.
    """
    if resp.status_code not in (200, 201):
        return False
    try:
        body = resp.json()
    except ValueError:
        # 200 sem JSON valido: assume sucesso (nao ha como saber mais).
        return True
    if isinstance(body, dict) and body.get("success") is False:
        return False
    return True


def _envelope_status(resp: requests.Response) -> int:
    """Status efetivo: o `status_code` do envelope quando presente."""
    try:
        body = resp.json()
    except ValueError:
        return resp.status_code
    if isinstance(body, dict) and body.get("success") is False:
        return int(body.get("status_code") or resp.status_code)
    return resp.status_code


def register_with_platform(
    manifest: ProductManifest,
    config: Optional[ProductRegistrationConfig] = None,
) -> Optional[Dict[str, Any]]:
    """Envia o manifesto via POST /product/register.

    Retries lineares em erros 5xx ou conexao recusada. Erros 4xx
    (manifesto invalido, chave errada) nao tem retry — corrigir e tentar
    de novo. Best-effort: loga e retorna None, nunca levanta excecao.

    Retorna o `data` da resposta ou None em falha (apos retries).
    """
    cfg = config or ProductRegistrationConfig.from_env()
    if not cfg.is_usable():
        logger.warning(
            "[registration] ADMIN_CENTER_URL nao configurado — registro pulado."
        )
        return None

    url = f"{cfg.platform_url}/product/register"
    payload = manifest.to_payload()

    last_error: Optional[str] = None
    for attempt in range(1, cfg.register_max_retries + 1):
        try:
            resp = requests.post(
                url,
                headers=_headers(cfg),
                json=payload,
                timeout=cfg.timeout_seconds,
            )
            if _envelope_ok(resp):
                logger.info(
                    "[registration] Produto '%s' (v%s) registrado em %s",
                    manifest.code, manifest.version, cfg.platform_url,
                )
                try:
                    return resp.json().get("data")
                except ValueError:
                    return None

            effective_status = _envelope_status(resp)

            # 4xx (HTTP ou no envelope): nao adianta retry. Loga e sai.
            if 400 <= effective_status < 500:
                logger.error(
                    "[registration] Falha 4xx ao registrar '%s' (status=%s): %s",
                    manifest.code, effective_status, resp.text[:500],
                )
                return None

            last_error = f"status={effective_status} body={resp.text[:200]}"
        except requests.RequestException as exc:
            last_error = str(exc)

        if attempt < cfg.register_max_retries:
            backoff = cfg.register_retry_backoff_seconds * attempt
            logger.warning(
                "[registration] Tentativa %s/%s falhou (%s). Retry em %.1fs...",
                attempt, cfg.register_max_retries, last_error, backoff,
            )
            time.sleep(backoff)

    logger.error(
        "[registration] Desistindo de registrar '%s' apos %s tentativas. Ultimo erro: %s",
        manifest.code, cfg.register_max_retries, last_error,
    )
    return None


def send_heartbeat(
    code: str,
    config: Optional[ProductRegistrationConfig] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> bool:
    """POST /product/{code}/heartbeat. Retorna True em sucesso.

    `payload` opcional: {'version'?: str, 'mode'?: 'test'|'live'}.
    """
    cfg = config or ProductRegistrationConfig.from_env()
    if not cfg.is_usable():
        return False
    url = f"{cfg.platform_url}/product/{code}/heartbeat"
    try:
        resp = requests.post(
            url,
            headers=_headers(cfg),
            json=payload or {},
            timeout=cfg.timeout_seconds,
        )
        if _envelope_ok(resp):
            return True
        effective_status = _envelope_status(resp)
        # 404 = produto nao registrado ainda. Sinal pra chamar /register novamente.
        if effective_status == 404:
            logger.warning(
                "[registration] Heartbeat de '%s' retornou 404 — produto nao registrado.",
                code,
            )
        else:
            logger.debug(
                "[registration] Heartbeat de '%s' falhou status=%s body=%s",
                code, effective_status, resp.text[:200],
            )
        return False
    except requests.RequestException as exc:
        logger.debug("[registration] Heartbeat de '%s' erro de conexao: %s", code, exc)
        return False


class _HeartbeatStopper:
    """Handle simples para sinalizar parada do loop e aguardar a thread."""
    def __init__(self, event: threading.Event, thread: threading.Thread):
        self._event = event
        self._thread = thread

    def __call__(self, timeout: float = 2.0) -> None:
        self._event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)


def start_heartbeat_loop(
    code: str,
    config: Optional[ProductRegistrationConfig] = None,
    interval_seconds: Optional[int] = None,
    payload_factory: Optional[Any] = None,
    auto_reregister_manifest: Optional[ProductManifest] = None,
) -> _HeartbeatStopper:
    """Inicia loop de heartbeat em thread daemon.

    Args:
        code: codigo do produto (mesmo do manifesto).
        config: config explicita. Se None, lida do ambiente.
        interval_seconds: override do intervalo. Default usa cfg.heartbeat_interval_seconds.
        payload_factory: callable() -> dict opcional, executado a cada
            tick para construir o payload (versao em runtime, mode, etc.).
        auto_reregister_manifest: se fornecido, e se um heartbeat falhar
            (ex.: 404 = produto sumiu do catalogo), reenvia o manifesto.

    Retorna um callable; chame-o no shutdown para parar a thread:

        stop = start_heartbeat_loop(...)
        ...
        stop()  # idempotente

    Chamar varias vezes cria varias threads — evite. Tipico e chamar uma
    unica vez no startup do produto (lifespan do FastAPI).
    """
    cfg = config or ProductRegistrationConfig.from_env()
    interval = interval_seconds or cfg.heartbeat_interval_seconds
    stop_event = threading.Event()

    def _loop() -> None:
        logger.info(
            "[registration] Heartbeat loop iniciado para '%s' (intervalo=%ss)",
            code, interval,
        )
        while not stop_event.is_set():
            try:
                payload = None
                if callable(payload_factory):
                    try:
                        payload = payload_factory() or None
                    except Exception:
                        logger.exception("[registration] payload_factory falhou; usando vazio.")
                        payload = None

                ok = send_heartbeat(code, cfg, payload)
                if not ok and auto_reregister_manifest is not None:
                    # 404 do heartbeat indica que o produto sumiu do catalogo
                    # (ex.: AdminCenter recriou banco em dev). Reenvia manifesto.
                    register_with_platform(auto_reregister_manifest, cfg)
            except Exception:
                logger.exception("[registration] Erro inesperado no heartbeat loop.")

            # Event.wait em vez de time.sleep para shutdown responsivo.
            stop_event.wait(timeout=interval)

        logger.info("[registration] Heartbeat loop de '%s' encerrado.", code)

    thread = threading.Thread(
        target=_loop,
        name=f"automaxia-heartbeat-{code}",
        daemon=True,
    )
    thread.start()
    return _HeartbeatStopper(stop_event, thread)
