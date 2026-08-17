"""Helper para aplicar migrations alembic no startup do FastAPI.

Uso tipico em qualquer backend do Studio (admincenter-api, dashboard-backend,
datachatai-api, turing-backend):

    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from automaxia_utils.migrations import run_migrations

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        run_migrations()
        yield

    app = FastAPI(lifespan=lifespan)

Por que na lib e nao em cada backend: os quatro repetiam o mesmo bloco de
`alembic upgrade head` no lifespan, cada um com um tratamento de erro
diferente. O ponto que sempre faltava era o RETRY — em k8s o pod sobe antes
do Postgres aceitar conexao, e sem retry o backend morria no boot com
OperationalError em vez de esperar o banco subir.

Variaveis de ambiente respeitadas:
- ALEMBIC_CONFIG_PATH: caminho do alembic.ini (default: ./alembic.ini)
- ALEMBIC_AUTO_UPGRADE: 'true'/'false' (default: 'true' — desabilita em testes)
- ALEMBIC_RETRY_MAX: tentativas em caso de banco indisponivel (default: 30)
- ALEMBIC_RETRY_DELAY: segundos entre tentativas (default: 2)
"""

from .runner import run_migrations

__all__ = ["run_migrations"]
