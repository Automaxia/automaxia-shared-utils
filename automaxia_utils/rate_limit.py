"""Rate limit por janela deslizante para os satelites (1.21.0, LIB-45 / ST-106).

Porte do middleware do AdminCenter (`admincenter-api/src/middleware/
rate_limiter.py`): Redis quando ha URL, memoria quando nao ha ou quando o
Redis oscila. Antes desta versao so o AdminCenter e o Talk limitavam
requisicoes — Vision, Harvest, Office, Viewer, Forge e Cartografo aceitavam
qualquer ritmo, inclusive nas rotas que chamam LLM (custo direto de token).

Uso no `server.py` do satelite, ANTES do CORSMiddleware (a pilha do
Starlette executa na ordem inversa: o CORS precisa ficar por fora para o 429
sair com os cabecalhos de CORS)::

    from automaxia_utils.rate_limit import Limite, RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware, limites=[
        Limite("/api/chat", 20),
        Limite("/api/produtos/*/montar", 10),
    ])

Identidade do chamador, nesta ordem:
  1. `user_id` (ou `sub`) do JWT, quando o Bearer e valido — limita a PESSOA,
     entao um escritorio atras de um NAT nao divide a mesma cota;
  2. hash da `X-API-Key`, quando houver (chamada de maquina);
  3. IP: o PRIMEIRO valor do `X-Forwarded-For`. No cluster o ingress-nginx
     sobrescreve o cabecalho com o `$remote_addr` (nao concatena o que o
     cliente mandou), e o nginx do front so acrescenta o IP dele no fim — o
     primeiro valor e o cliente real e nao da para forjar pela internet.

Contagem: cada `Limite` tem a sua (por identidade); o resto da API divide a
cota padrao. Uma tela que faz polling nao gasta a cota da rota de LLM.

Configuracao por ambiente — lida na PRIMEIRA requisicao, nao no import: o
satelite carrega as variaveis do AdminCenter no startup, depois que o
middleware ja foi montado.
  RATE_LIMIT_ENABLED        true | false               (padrao true)
  RATE_LIMIT_REQUESTS       cota padrao por minuto      (padrao do construtor, 300)
  RATE_LIMIT_REDIS_URL      Redis compartilhado; cai para REDIS_URL. Sem
                            nenhum dos dois o limite vale por WORKER — com o
                            gunicorn em N workers, o teto efetivo e N vezes.

FAIL-OPEN: Redis fora do ar nao derruba a API (cai para memoria), e qualquer
erro inesperado do proprio limitador deixa a requisicao passar.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

#: Nunca contam: probes do Kubernetes (batem em intervalo fixo e gastariam a
#: cota do IP do kubelet) e documentacao.
ISENTOS_PADRAO: Tuple[str, ...] = (
    "/health", "/readiness", "/ready", "/metrics",
    "/docs", "/redoc", "/openapi.json", "/api/docs", "/api/redoc", "/api/openapi.json",
)

#: Acima disso o backend em memoria descarta as chaves frias — sem teto, cada
#: IP distinto de uma varredura deixaria um bucket para sempre.
_MAX_BUCKETS = 50_000


@dataclass
class Limite:
    """Cota de uma rota (ou grupo de rotas).

    `rota` e um padrao de caminho: `*` casa um segmento e `**` casa qualquer
    resto (`/api/p/**`). Sem curinga, casa o caminho exato. O primeiro
    `Limite` que casar vence.
    """

    rota: str
    maximo: int
    janela: int = 60
    _regex: "re.Pattern[str]" = field(init=False, repr=False)

    def __post_init__(self):
        partes = re.escape(self.rota).replace(r"\*\*", ".*").replace(r"\*", "[^/]+")
        self._regex = re.compile(partes)

    def casa(self, path: str) -> bool:
        return self._regex.fullmatch(path) is not None


class _BackendMemoria:
    """Janela deslizante em memoria do processo."""

    def __init__(self):
        self._buckets: dict = defaultdict(list)
        self._lock = asyncio.Lock()

    async def hit(self, key: str, maximo: int, janela: int) -> Tuple[bool, int, int]:
        agora = time.time()
        inicio = agora - janela
        async with self._lock:
            bucket = self._buckets[key]
            bucket[:] = [ts for ts in bucket if ts > inicio]
            reset = int((bucket[0] if bucket else agora) + janela)
            if len(bucket) >= maximo:
                return False, 0, reset
            bucket.append(agora)
            self._podar(inicio)
            return True, max(maximo - len(bucket), 0), reset

    def _podar(self, inicio: float) -> None:
        if len(self._buckets) <= _MAX_BUCKETS:
            return
        mortos = [k for k, v in self._buckets.items() if not v or v[-1] <= inicio]
        for k in mortos:
            del self._buckets[k]


class _BackendRedis:
    """Janela deslizante com sorted set — vale entre workers e replicas."""

    def __init__(self, url: str, prefixo: str):
        self._url = url
        self._prefixo = prefixo
        self._redis = None

    async def _client(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                self._url, decode_responses=True,
                socket_connect_timeout=2, socket_timeout=2,
            )
        return self._redis

    async def hit(self, key: str, maximo: int, janela: int) -> Tuple[bool, int, int]:
        agora = time.time()
        rkey = f"rl:{self._prefixo}:{key}"
        membro = f"{agora}:{id(object())}"
        client = await self._client()
        pipe = client.pipeline(transaction=True)
        pipe.zremrangebyscore(rkey, 0, agora - janela)
        pipe.zcard(rkey)
        pipe.zadd(rkey, {membro: agora})
        pipe.expire(rkey, janela + 1)
        atual = (await pipe.execute())[1]  # zcard ANTES desta requisicao
        reset = int(agora + janela)
        if atual >= maximo:
            # Desfaz a entrada recusada, senao a janela nunca esvazia
            # enquanto o cliente insistir.
            await client.zrem(rkey, membro)
            return False, 0, reset
        return True, max(maximo - atual - 1, 0), reset


def _decodificar_jwt(token: str, segredo: str, algoritmo: str) -> Optional[dict]:
    """Payload do JWT ou None. Aceita python-jose ou PyJWT — cada satelite
    tem um dos dois."""
    try:
        from jose import jwt as jose_jwt
        return jose_jwt.decode(token, segredo, algorithms=[algoritmo],
                               options={"verify_aud": False})
    except ImportError:
        pass
    except Exception:
        return None
    try:
        import jwt as pyjwt
        return pyjwt.decode(token, segredo, algorithms=[algoritmo],
                            options={"verify_aud": False})
    except Exception:
        return None


def _env_bool(nome: str, padrao: bool) -> bool:
    valor = os.getenv(nome)
    if valor is None or not valor.strip():
        return padrao
    return valor.strip().lower() not in ("0", "false", "no", "off", "nao")


def _env_int(nome: str, padrao: int) -> int:
    try:
        return max(1, int(os.getenv(nome) or padrao))
    except ValueError:
        return padrao


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Limita requisicoes por chamador. Ver docstring do modulo."""

    def __init__(
        self,
        app,
        limites: Sequence[Limite] = (),
        padrao: int = 300,
        janela: int = 60,
        isentos: Iterable[str] = (),
    ):
        super().__init__(app)
        self._limites: List[Limite] = list(limites)
        self._padrao_construtor = padrao
        self._janela = janela
        self._isentos = tuple(ISENTOS_PADRAO) + tuple(isentos)
        self._configurado = False
        self._habilitado = True
        self._padrao = padrao
        self._segredo = ""
        self._algoritmo = "HS256"
        self._memoria = _BackendMemoria()
        self._redis: Optional[_BackendRedis] = None

    def _configurar(self) -> None:
        """Le o ambiente na primeira requisicao (ver docstring do modulo)."""
        self._configurado = True
        self._habilitado = _env_bool("RATE_LIMIT_ENABLED", True)
        self._padrao = _env_int("RATE_LIMIT_REQUESTS", self._padrao_construtor)
        self._segredo = os.getenv("SECRET_KEY", os.getenv("JWT_SECRET_KEY", ""))
        self._algoritmo = os.getenv("ALGORITHM", "HS256")
        url = (os.getenv("RATE_LIMIT_REDIS_URL") or os.getenv("REDIS_URL") or "").strip()
        if url:
            try:
                import redis.asyncio  # noqa: F401
                prefixo = os.getenv("ADMIN_CENTER_PRODUCT_SLUG") or "app"
                self._redis = _BackendRedis(url, prefixo)
                logger.info("rate limit: backend Redis")
            except ImportError:
                logger.warning("rate limit: REDIS_URL definida mas o pacote redis nao esta instalado — usando memoria")
        else:
            logger.info("rate limit: sem Redis — limite por worker (o teto efetivo e multiplicado pelos workers)")
        if not self._habilitado:
            logger.warning("rate limit: DESLIGADO por RATE_LIMIT_ENABLED")

    def _identidade(self, request: Request) -> str:
        auth = request.headers.get("authorization") or ""
        if self._segredo and auth[:7].lower() == "bearer ":
            payload = _decodificar_jwt(auth[7:].strip(), self._segredo, self._algoritmo)
            if payload:
                ident = payload.get("user_id") or payload.get("sub")
                if ident:
                    return f"user:{ident}"
        chave = request.headers.get("x-api-key")
        if chave:
            return "key:" + hashlib.sha256(chave.encode()).hexdigest()[:24]
        encaminhado = request.headers.get("x-forwarded-for")
        if encaminhado:
            return "ip:" + encaminhado.split(",")[0].strip()
        return "ip:" + (request.client.host if request.client else "desconhecido")

    def _regra(self, path: str) -> Tuple[str, int, int]:
        """(grupo da contagem, maximo, janela)."""
        for limite in self._limites:
            if limite.casa(path):
                return limite.rota, limite.maximo, limite.janela
        return "*", self._padrao, self._janela

    async def dispatch(self, request: Request, call_next):
        if not self._configurado:
            self._configurar()
        path = request.url.path
        # Preflight de CORS o browser dispara sozinho — nao gasta a cota.
        if (not self._habilitado or request.method == "OPTIONS"
                or any(path == p or path.startswith(p + "/") for p in self._isentos)):
            return await call_next(request)

        try:
            grupo, maximo, janela = self._regra(path)
            chave = f"{self._identidade(request)}|{grupo}"
            permitido, restante, reset = await self._hit(chave, maximo, janela)
        except Exception as exc:  # o limitador nunca derruba a requisicao
            logger.warning("rate limit: falha ao contar, deixando passar: %s", exc)
            return await call_next(request)

        cabecalhos = {
            "X-RateLimit-Limit": str(maximo),
            "X-RateLimit-Remaining": str(restante),
            "X-RateLimit-Reset": str(reset),
        }
        if not permitido:
            espera = max(reset - int(time.time()), 1)
            logger.warning("rate limit estourado: %s em %s (%d/%ds)", chave, path, maximo, janela)
            mensagem = f"Muitas requisicoes. Tente de novo em {espera}s."
            return JSONResponse(
                status_code=429,
                content={"detail": mensagem, "message": mensagem, "retry_after": espera},
                headers={**cabecalhos, "Retry-After": str(espera)},
            )

        response = await call_next(request)
        for nome, valor in cabecalhos.items():
            response.headers[nome] = valor
        return response

    async def _hit(self, chave: str, maximo: int, janela: int) -> Tuple[bool, int, int]:
        if self._redis is not None:
            try:
                return await self._redis.hit(chave, maximo, janela)
            except Exception as exc:
                logger.warning("rate limit: Redis indisponivel, caindo para memoria: %s", exc)
                self._redis = None  # sem isso cada requisicao pagaria o timeout
        return await self._memoria.hit(chave, maximo, janela)


__all__ = ["Limite", "RateLimitMiddleware", "ISENTOS_PADRAO"]
