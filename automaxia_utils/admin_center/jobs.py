"""JobRunner: cliente da lib que consome AdminCenter para agendamento.

Modelo:
- No boot, lib carrega lista de jobs do produto via GET /agent/job
- Mantem APScheduler local com base nos crons (resiliente a queda do AdminCenter)
- Reporta cada execucao via POST /agent/job/{id}/run, progresso via PATCH e
  conclusao via POST .../finish
- Servidor HTTP minusculo opcional (FastAPI) recebe webhooks do AdminCenter
  para acoes manuais — "rodar agora", "pausar", "retomar". Webhooks sao
  validados via HMAC-SHA256 (header X-AdminCenter-Signature)
- Reload automatico de config: o handler de webhook chama reload_jobs() apos
  qualquer comando, garantindo que mudancas de cron propaguem em <1s

Uso tipico:
    from automaxia_utils import get_admin_center_service
    from automaxia_utils.admin_center.jobs import JobRunner

    runner = JobRunner(get_admin_center_service())
    runner.register("rpa_boletos.rodada",  _do_rodada)
    runner.register("rpa_boletos.relatorio", _do_relatorio)
    runner.start()  # bloqueia (APScheduler) + sobe webhook server em thread

    # dentro do _do_rodada, opcional:
    # runner.report_progress(percent=30, message="Baixando boletos")
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Any

import requests

logger = logging.getLogger(__name__)


# ---------- Endpoints ----------

class _Endpoints:
    AGENT_LIST = "/agent/job"
    AGENT_RUN = "/agent/job/{job_id}/run"
    RUN_PROGRESS = "/agent/job/run/{run_id}/progress"
    RUN_FINISH = "/agent/job/run/{run_id}/finish"


# ---------- Config / dados ----------

@dataclass
class _JobConfig:
    id: str
    slug: str
    name: str
    cron_expression: str
    timezone: str
    is_enabled: bool
    max_instances: int
    timeout_seconds: Optional[int]
    config_version: int
    force_run_at: Optional[str]
    status: str

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "_JobConfig":
        return cls(
            id=str(d["id"]),
            slug=d["slug"],
            name=d.get("name") or d["slug"],
            cron_expression=d["cron_expression"],
            timezone=d.get("timezone") or "America/Sao_Paulo",
            is_enabled=bool(d.get("is_enabled", True)),
            max_instances=int(d.get("max_instances") or 1),
            timeout_seconds=d.get("timeout_seconds"),
            config_version=int(d.get("config_version") or 1),
            force_run_at=d.get("force_run_at"),
            status=d.get("status") or "active",
        )


@dataclass
class _RunContext:
    """Contexto de uma execucao em andamento. Thread-local para suportar
    multiplos jobs em paralelo no mesmo processo."""
    run_id: str
    job_id: str
    job_slug: str
    started_at: float
    triggered_by: str = "cron"


# ---------- JobRunner ----------

class JobRunner:
    """Coordena agendamento local + reporte ao AdminCenter."""

    def __init__(self, admin_service, polling_interval: int = 60):
        """
        Args:
            admin_service: instancia do AdminCenterService (singleton da lib)
            polling_interval: segundos entre polls de fallback (so usado quando
                              webhook nao esta disponivel ou para detectar
                              `force_run_at` que tenha escapado do webhook)
        """
        self.svc = admin_service
        self.polling_interval = polling_interval

        # Funcoes registradas pelo produto: { slug: callable }
        self._handlers: Dict[str, Callable[[], None]] = {}

        # Cache de configuracao
        self._jobs: Dict[str, _JobConfig] = {}  # slug -> config
        self._jobs_by_id: Dict[str, _JobConfig] = {}  # id -> config
        self._lock = threading.RLock()

        # Run context — armazenado em variavel de thread local para que
        # report_progress() do produto pegue automaticamente
        self._run_ctx_local = threading.local()

        # APScheduler
        self._scheduler = None
        self._scheduler_started = False

        # Webhook server
        self._webhook_thread: Optional[threading.Thread] = None
        self._webhook_secret = os.getenv("ADMIN_CENTER_JOBS_WEBHOOK_SECRET", "")
        self._webhook_port = int(os.getenv("ADMIN_CENTER_JOBS_WEBHOOK_PORT", "8001"))

        # Polling thread
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ---------- Registro de handlers ----------

    def register(self, slug: str, handler: Callable[[], None]) -> None:
        """Vincula um slug de job a uma funcao Python."""
        self._handlers[slug] = handler
        logger.info("JobRunner: handler registrado para %s", slug)

    # ---------- Carregamento de config ----------

    def reload_jobs(self) -> None:
        """Re-busca a lista de jobs no AdminCenter e reconfigura APScheduler."""
        if not self.svc or not getattr(self.svc, 'config', None) or not self.svc.config.enabled:
            logger.warning("JobRunner: AdminCenter desabilitado, nada a recarregar")
            return

        try:
            params = {"product_id": self.svc.config.product_id}
            if self.svc.environment_id:
                params["environment_id"] = self.svc.environment_id
            response = self.svc._make_request("GET", _Endpoints.AGENT_LIST, params=params)
        except Exception as e:
            logger.error("JobRunner: erro ao listar jobs: %s", e)
            return

        if not response or "data" not in response:
            logger.warning("JobRunner: resposta vazia do AdminCenter")
            return

        new_jobs: Dict[str, _JobConfig] = {}
        new_by_id: Dict[str, _JobConfig] = {}
        for item in response["data"] or []:
            try:
                cfg = _JobConfig.from_dict(item)
                new_jobs[cfg.slug] = cfg
                new_by_id[cfg.id] = cfg
            except Exception as e:
                logger.warning("JobRunner: job invalido recebido: %s — %s", item, e)

        with self._lock:
            self._jobs = new_jobs
            self._jobs_by_id = new_by_id

        logger.info("JobRunner: %d job(s) carregado(s)", len(new_jobs))
        self._reschedule_all()

    # ---------- APScheduler ----------

    def _ensure_scheduler(self):
        if self._scheduler is not None:
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger  # noqa
        except ImportError as e:
            logger.error("APScheduler nao instalado: %s. Adicione apscheduler ao requirements do produto.", e)
            raise
        self._scheduler = BackgroundScheduler()

    def _reschedule_all(self) -> None:
        if not self._scheduler:
            return

        from apscheduler.triggers.cron import CronTrigger

        # Remove todos os jobs anteriores; re-adiciona somente os ativos+habilitados+com handler
        self._scheduler.remove_all_jobs()
        with self._lock:
            jobs_snapshot = list(self._jobs.values())

        scheduled = 0
        for cfg in jobs_snapshot:
            if cfg.status != 'active' or not cfg.is_enabled:
                continue
            if cfg.slug not in self._handlers:
                logger.debug("JobRunner: ignorando %s (sem handler local)", cfg.slug)
                continue
            try:
                trigger = CronTrigger.from_crontab(cfg.cron_expression, timezone=cfg.timezone)
                self._scheduler.add_job(
                    func=self._wrap_for_scheduler(cfg),
                    trigger=trigger,
                    id=cfg.id,
                    name=cfg.slug,
                    max_instances=cfg.max_instances,
                    coalesce=True,
                    replace_existing=True,
                )
                scheduled += 1
            except Exception as e:
                logger.error("JobRunner: erro ao agendar %s (cron='%s'): %s",
                             cfg.slug, cfg.cron_expression, e)

        logger.info("JobRunner: %d job(s) agendado(s) localmente", scheduled)

    def _wrap_for_scheduler(self, cfg: _JobConfig) -> Callable:
        """Cria closure que dispara um job pelo APScheduler (origem 'cron')."""
        def _runner():
            self.run_job(cfg.slug, triggered_by="cron")
        return _runner

    # ---------- Disparo manual / por webhook ----------

    def run_job(self, slug: str, triggered_by: str = "manual") -> bool:
        """Executa o job referente ao slug. Reporta inicio/progresso/fim ao AdminCenter."""
        with self._lock:
            cfg = self._jobs.get(slug)

        if not cfg:
            logger.error("JobRunner: job %s nao encontrado", slug)
            return False
        if cfg.slug not in self._handlers:
            logger.error("JobRunner: handler para %s nao registrado", slug)
            return False
        if not cfg.is_enabled or cfg.status != 'active':
            logger.warning("JobRunner: job %s desabilitado ou pausado, pulando", slug)
            return False

        # Cria run no AdminCenter
        run_id = self._create_run(cfg.id, triggered_by)
        if not run_id:
            # Mesmo sem run_id, tenta executar (modo offline) — reporta no fim se houver chance
            logger.warning("JobRunner: falha ao criar run no AdminCenter, executando localmente sem tracking")

        ctx = _RunContext(
            run_id=run_id or "",
            job_id=cfg.id,
            job_slug=cfg.slug,
            started_at=time.time(),
            triggered_by=triggered_by,
        )
        # Disponibiliza o contexto para report_progress no thread atual
        self._run_ctx_local.ctx = ctx

        try:
            self._handlers[slug]()
        except Exception as e:
            duration_ms = int((time.time() - ctx.started_at) * 1000)
            logger.exception("JobRunner: job %s falhou", slug)
            if run_id:
                self._finish_run(run_id, status="failed",
                                 duration_ms=duration_ms,
                                 error_message=str(e))
            return False
        else:
            duration_ms = int((time.time() - ctx.started_at) * 1000)
            if run_id:
                self._finish_run(run_id, status="completed", duration_ms=duration_ms)
            logger.info("JobRunner: job %s concluido em %dms", slug, duration_ms)
            return True
        finally:
            self._run_ctx_local.ctx = None

    # ---------- Reportes para AdminCenter ----------

    def _create_run(self, job_id: str, triggered_by: str) -> Optional[str]:
        try:
            response = self.svc._make_request(
                "POST",
                _Endpoints.AGENT_RUN.format(job_id=job_id),
                {"triggered_by": triggered_by, "input_data": {}}
            )
            if response and "data" in response and response["data"]:
                return str(response["data"].get("id") or response["data"].get("run_id") or "")
        except Exception as e:
            logger.warning("JobRunner: erro ao criar run: %s", e)
        return None

    def report_progress(self, percent: int, message: Optional[str] = None,
                         step_name: Optional[str] = None) -> None:
        """Pode ser chamado por dentro do handler do job. Pega o run_id
        do contexto thread-local automaticamente."""
        ctx = getattr(self._run_ctx_local, 'ctx', None)
        if not ctx or not ctx.run_id:
            return
        try:
            self.svc._make_request(
                "PATCH",
                _Endpoints.RUN_PROGRESS.format(run_id=ctx.run_id),
                {"percent": max(0, min(100, percent)), "message": message, "step_name": step_name}
            )
        except Exception as e:
            logger.debug("JobRunner: erro ao reportar progresso: %s", e)

    def _finish_run(self, run_id: str, status: str, duration_ms: int = 0,
                    error_message: Optional[str] = None,
                    output_data: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.svc._make_request(
                "POST",
                _Endpoints.RUN_FINISH.format(run_id=run_id),
                {
                    "status": status,
                    "duration_ms": duration_ms,
                    "error_message": error_message,
                    "output_data": output_data or {},
                }
            )
        except Exception as e:
            logger.warning("JobRunner: erro ao finalizar run: %s", e)

    # ---------- Webhook server ----------

    def _start_webhook_server(self, host: str = "0.0.0.0") -> None:
        """Sobe um servidor HTTP minimalista (stdlib) para receber comandos do
        AdminCenter. Usa stdlib (http.server) para nao impor FastAPI ao produto."""
        from http.server import HTTPServer, BaseHTTPRequestHandler

        runner_ref = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # silencia logs verbosos
                logger.debug("[Webhook] " + format, *args)

            def _read_body(self) -> bytes:
                length = int(self.headers.get('Content-Length', '0') or 0)
                return self.rfile.read(length) if length else b''

            def _verify_signature(self, body: bytes) -> bool:
                if not runner_ref._webhook_secret:
                    return True  # sem secret configurado, aceita (modo dev)
                received = self.headers.get('X-AdminCenter-Signature', '')
                expected = hmac.new(
                    runner_ref._webhook_secret.encode('utf-8'),
                    body,
                    hashlib.sha256
                ).hexdigest()
                return hmac.compare_digest(received, expected)

            def do_POST(self):
                if self.path != '/control':
                    self.send_response(404)
                    self.end_headers()
                    return
                body = self._read_body()
                if not self._verify_signature(body):
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"invalid signature"}')
                    return

                try:
                    payload = json.loads(body or b'{}')
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
                    return

                event = payload.get('event')
                slug = None
                # Resolve slug a partir do job_id quando o evento tras job_id
                job_id = payload.get('job_id')
                if job_id:
                    cfg = runner_ref._jobs_by_id.get(job_id)
                    if cfg:
                        slug = cfg.slug

                logger.info("[Webhook] %s recebido para %s", event, slug or job_id)

                if event == 'job.run_now' and slug:
                    # Roda em thread separada para responder o webhook rapido
                    threading.Thread(
                        target=runner_ref.run_job,
                        args=(slug, 'manual'),
                        daemon=True
                    ).start()
                elif event in ('job.paused', 'job.resumed', 'job.config_changed'):
                    threading.Thread(
                        target=runner_ref.reload_jobs,
                        daemon=True
                    ).start()

                self.send_response(202)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"accepted": true}')

            def do_GET(self):
                if self.path == '/control/health':
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')
                else:
                    self.send_response(404)
                    self.end_headers()

        server = HTTPServer((host, self._webhook_port), _Handler)

        def _serve():
            logger.info("JobRunner webhook server escutando em %s:%d", host, self._webhook_port)
            try:
                server.serve_forever()
            except Exception as e:
                logger.error("JobRunner webhook server caiu: %s", e)

        self._webhook_thread = threading.Thread(target=_serve, daemon=True, name="JobRunnerWebhook")
        self._webhook_thread.start()

    # ---------- Polling de fallback ----------

    def _start_polling(self) -> None:
        def _loop():
            last_versions: Dict[str, int] = {}
            while not self._stop_event.is_set():
                try:
                    self.reload_jobs()
                    # Detecta force_run_at recem-setado
                    with self._lock:
                        for slug, cfg in self._jobs.items():
                            if cfg.force_run_at and slug in self._handlers:
                                # Compara com versao previa para nao re-executar
                                prev_version = last_versions.get(slug, 0)
                                if cfg.config_version > prev_version:
                                    last_versions[slug] = cfg.config_version
                                    threading.Thread(
                                        target=self.run_job,
                                        args=(slug, 'manual'),
                                        daemon=True
                                    ).start()
                except Exception as e:
                    logger.warning("JobRunner polling: %s", e)
                self._stop_event.wait(self.polling_interval)

        self._poll_thread = threading.Thread(target=_loop, daemon=True, name="JobRunnerPoll")
        self._poll_thread.start()

    # ---------- Lifecycle ----------

    def start(self, with_webhook_server: bool = True, with_polling: bool = True,
              block: bool = True) -> None:
        """Inicializa scheduler local + servidor de webhook + polling.

        Args:
            with_webhook_server: sobe servidor HTTP em ADMIN_CENTER_JOBS_WEBHOOK_PORT
            with_polling: sobe thread de fallback que reconfere config a cada N segundos
            block: bloqueia a thread principal (igual APScheduler.BlockingScheduler).
                   Se False, retorna logo apos iniciar (para uso em apps com seu
                   proprio loop principal).
        """
        if not self.svc or not getattr(self.svc, 'config', None) or not self.svc.config.enabled:
            logger.warning("JobRunner.start: AdminCenter desabilitado — nenhum job sera executado")
            return

        self._ensure_scheduler()
        self.reload_jobs()
        if not self._scheduler_started:
            self._scheduler.start()
            self._scheduler_started = True
            logger.info("JobRunner: APScheduler iniciado")

        if with_webhook_server:
            try:
                self._start_webhook_server()
            except Exception as e:
                logger.warning("JobRunner: falha ao subir webhook server: %s — seguindo sem ele", e)

        if with_polling:
            self._start_polling()

        if block:
            try:
                while not self._stop_event.is_set():
                    time.sleep(1)
            except (KeyboardInterrupt, SystemExit):
                logger.info("JobRunner: shutdown solicitado")
            finally:
                self.shutdown()

    def shutdown(self) -> None:
        self._stop_event.set()
        try:
            if self._scheduler and self._scheduler_started:
                self._scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            if self.svc and hasattr(self.svc, 'shutdown'):
                self.svc.shutdown()
        except Exception:
            pass
