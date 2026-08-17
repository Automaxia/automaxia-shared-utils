"""Runner de alembic com retry — usado pelo lifespan dos backends."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def run_migrations(
    config_path: str | None = None,
    target: str = "head",
) -> None:
    """Aplica `alembic upgrade <target>` com retry tolerante a banco indisponivel.

    Args:
        config_path: caminho do alembic.ini. Default: ALEMBIC_CONFIG_PATH env ou ./alembic.ini.
        target: revisao alvo (default 'head').

    Levanta a ultima excecao se todas as tentativas falharem.
    """
    if not _is_enabled():
        logger.info("ALEMBIC_AUTO_UPGRADE=false — skip de migracoes.")
        return

    cfg_path = config_path or os.getenv("ALEMBIC_CONFIG_PATH", "alembic.ini")
    cfg_file = Path(cfg_path)
    if not cfg_file.exists():
        raise FileNotFoundError(f"alembic.ini nao encontrado em {cfg_file.resolve()}")

    retry_max = int(os.getenv("ALEMBIC_RETRY_MAX", "30"))
    retry_delay = float(os.getenv("ALEMBIC_RETRY_DELAY", "2"))

    from alembic import command
    from alembic.config import Config

    last_error: Exception | None = None
    for attempt in range(1, retry_max + 1):
        try:
            alembic_cfg = Config(str(cfg_file))
            logger.info(
                "Aplicando migrations (alembic upgrade %s) — tentativa %d/%d",
                target, attempt, retry_max,
            )
            command.upgrade(alembic_cfg, target)
            logger.info("Migrations aplicadas com sucesso.")
            return
        except Exception as exc:  # OperationalError, ProgrammingError, etc.
            last_error = exc
            logger.warning(
                "Migration falhou (%s). Aguardando %.1fs antes da proxima tentativa.",
                exc, retry_delay,
            )
            time.sleep(retry_delay)

    logger.error("Migrations falharam apos %d tentativas.", retry_max)
    raise last_error  # type: ignore[misc]


def _is_enabled() -> bool:
    return os.getenv("ALEMBIC_AUTO_UPGRADE", "true").lower() not in ("false", "0", "no")
