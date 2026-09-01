"""Resolver de conexoes de banco de dados — broker do AdminCenter.

Cada produto que precisa de credencial de banco de dados nao guarda mais
host/porta/usuario/senha em variavel de ambiente local. Chama o
AdminCenter (`GET /api/database-connection/resolve`) que devolve as
credenciais decriptadas com TTL de 5 minutos.

Fluxo:

    from automaxia_utils import get_admin_center_service
    admin = get_admin_center_service()

    # SQLAlchemy (recomendado)
    with admin.get_db_session('casan_prod') as session:
        rows = session.execute(text('select * from sabesp.clientes')).fetchall()

    # psycopg2 direto
    conn = admin.get_db_connection('metricas')
    with conn.cursor() as cur:
        cur.execute('SELECT 1')

    # Acesso ao DTO bruto (host, port, user, password decriptados)
    resolved = admin.resolve_connection(alias='casan_prod')

Cache:
    Resultados de `/resolve` ficam em memoria por `expires_at` (default 5
    min). Quando o admincenter retorna `version` diferente do cacheado,
    a entrada e' invalidada — produtos pegam credenciais novas sem precisar
    reiniciar.

Tunel:
    Se a conexao tem `use_tunnel=true` e `tunnel_type='ssh'`, o resolver
    abre `sshtunnel.SSHTunnelForwarder` localmente e aponta o engine para
    `127.0.0.1:<porta_local>`. Para Cloudflare Access, espera-se que um
    `cloudflared access tcp` esteja rodando no host do produto e
    `tunnel_config.local_host`/`local_port` apontem para ele.

Lazy imports:
    psycopg2, sqlalchemy e sshtunnel sao importados sob demanda. Produtos
    que so usam `resolve_connection()` (e abrem a conexao por conta
    propria) nao precisam dessas deps instaladas.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote_plus


logger = logging.getLogger(__name__)


# ============================================================
# DTO — espelha ResolvedConnection do admincenter-api
# ============================================================
@dataclass
class ResolvedConnection:
    """Snapshot decriptado de uma conexao do cofre do AdminCenter.

    Espelha `models.ResolvedConnection` do admincenter-api. Mantem
    `expires_at` para cache TTL e `version` para invalidacao via /resolve.

    Suporta tres familias de engine — por isso host/port/database_name sao
    OPCIONAIS:
      - SQL (`postgresql`/`mysql`/`mssql`/`oracle`): host, port,
        database_name, username, password.
      - `databricks`: host (workspace) + `databricks_http_path`, password
        (= PAT); `username` vem vazio.
      - `rest`/`arcgis`: `base_url` + `auth_type`, password (= token);
        `username` vazio e host/port/database_name AUSENTES no payload.

    ⚠️ Ate a 1.13.0 os tres eram obrigatorios e lidos com `data["host"]` —
    resolver uma conexao rest/arcgis levantava KeyError dentro do `from_dict`,
    que o `_fetch` reportava como "payload invalido" e devolvia None. O
    produto via "conexao nao encontrada ou sem permissao" para uma conexao que
    existia e estava liberada.
    """
    id: str
    alias: str
    engine: str
    schema_name: str
    username: str
    password: str
    use_tunnel: bool
    version: int
    expires_at: datetime
    # Campos SQL/databricks — ausentes em rest/arcgis.
    host: Optional[str] = None
    port: Optional[int] = None
    database_name: Optional[str] = None
    # Engines HTTP (rest/arcgis/databricks)
    base_url: Optional[str] = None
    auth_type: Optional[str] = None
    databricks_http_path: Optional[str] = None
    arcgis_config: Optional[Dict[str, Any]] = None
    # Semantica que a conexao expoe ao produto consumidor. Quando preenchidas,
    # o produto deve usar estes nomes em vez de defaults hardcoded. Os
    # satelites leem via `getattr(resolved, campo, None)`: campo ausente NAO
    # da erro, cai no default e o produto degrada em silencio — por isso todo
    # campo que o /resolve devolve precisa existir aqui.
    ai_context: Optional[str] = None
    events_table: Optional[str] = None
    operational_table: Optional[str] = None
    tunnel_type: Optional[str] = None
    tunnel_config: Optional[Dict[str, Any]] = None
    access_level: str = "read"
    allowed_schemas: Optional[List[str]] = None
    allowed_tables: Optional[List[str]] = None
    denied_statements: Optional[List[str]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResolvedConnection":
        """Constroi o DTO a partir do payload da API."""
        expires_raw = data.get("expires_at")
        if isinstance(expires_raw, str):
            expires_at = _parse_iso(expires_raw)
        elif isinstance(expires_raw, datetime):
            expires_at = expires_raw
        else:
            expires_at = datetime.now(timezone.utc)

        port_raw = data.get("port")
        port = int(port_raw) if port_raw is not None else None

        return cls(
            id=str(data["id"]),
            alias=data["alias"],
            engine=data["engine"],
            host=data.get("host"),
            port=port,
            database_name=data.get("database_name"),
            # Fallback `public` vale para o banco do CLIENTE quando o cofre nao
            # traz `schema_name`. Nao confundir com o schema da plataforma.
            schema_name=data.get("schema_name") or "public",
            username=data.get("username") or "",
            password=data["password"],
            base_url=data.get("base_url"),
            auth_type=data.get("auth_type"),
            databricks_http_path=data.get("databricks_http_path"),
            arcgis_config=data.get("arcgis_config"),
            ai_context=data.get("ai_context"),
            events_table=data.get("events_table"),
            operational_table=data.get("operational_table"),
            use_tunnel=bool(data.get("use_tunnel", False)),
            tunnel_type=data.get("tunnel_type"),
            tunnel_config=data.get("tunnel_config"),
            access_level=data.get("access_level") or "read",
            allowed_schemas=data.get("allowed_schemas"),
            allowed_tables=data.get("allowed_tables"),
            denied_statements=data.get("denied_statements"),
            version=int(data.get("version", 1)),
            expires_at=expires_at,
        )

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    def dsn(self) -> str:
        """SQLAlchemy DSN para engines SQL. Usa driver psycopg2 para postgres.

        Levanta ValueError para engines que nao tem DSN (rest/arcgis/
        databricks) — melhor um erro nomeado aqui do que um DSN com
        `None:None` chegando ao driver.
        """
        if self.engine in ("rest", "arcgis", "databricks"):
            raise ValueError(
                f"engine='{self.engine}' nao tem DSN SQL. "
                "Use base_url / databricks_http_path."
            )
        if self.host is None or self.port is None or self.database_name is None:
            raise ValueError(
                f"engine='{self.engine}' exige host, port e database_name preenchidos"
            )
        if self.engine == "postgresql":
            scheme = "postgresql+psycopg2"
        elif self.engine == "mysql":
            scheme = "mysql+pymysql"
        elif self.engine == "mssql":
            scheme = "mssql+pyodbc"
        else:
            scheme = self.engine
        user = quote_plus(self.username)
        pwd = quote_plus(self.password)
        return f"{scheme}://{user}:{pwd}@{self.host}:{self.port}/{self.database_name}"


def _parse_iso(value: str) -> datetime:
    """Parse ISO-8601 com 'Z' final ou offset explicito."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        # Fallback: assume agora se nao parsear
        return datetime.now(timezone.utc)


# ============================================================
# ConnectionResolver — cache + tunel + materializacao
# ============================================================
@dataclass
class _CacheEntry:
    resolved: ResolvedConnection
    cached_at: float = field(default_factory=time.time)


class ConnectionResolver:
    """Resolve aliases em `ResolvedConnection`, mantem cache TTL e abre
    tuneis SSH/Cloudflare quando necessario.

    Threadsafe: chamadas concorrentes ao mesmo alias bloqueiam no
    `_lock` para evitar abrir N tuneis SSH em paralelo.
    """

    def __init__(self, admin_service: Any, default_ttl_fallback: int = 300):
        self._admin = admin_service
        self._cache: Dict[str, _CacheEntry] = {}
        self._tunnels: Dict[str, Any] = {}     # alias -> SSHTunnelForwarder
        self._lock = threading.Lock()
        self._default_ttl_fallback = default_ttl_fallback

    # ----------------------------------------------------------
    # Resolucao
    # ----------------------------------------------------------
    def resolve(
        self,
        alias: Optional[str] = None,
        connection_id: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Optional[ResolvedConnection]:
        """Devolve `ResolvedConnection` com credenciais decriptadas.

        Sem rede quando o cache esta valido. `force_refresh=True` ignora
        o cache (util apos detectar `version` mismatch).
        """
        if not alias and not connection_id:
            raise ValueError("Informe alias ou connection_id")

        cache_key = alias or f"id:{connection_id}"

        with self._lock:
            if not force_refresh:
                entry = self._cache.get(cache_key)
                if entry and not entry.resolved.is_expired():
                    return entry.resolved

        resolved = self._fetch(alias=alias, connection_id=connection_id)
        if not resolved:
            return None

        with self._lock:
            old = self._cache.get(cache_key)
            # Se versao mudou, derruba tunel anterior (credenciais novas)
            if old and old.resolved.version != resolved.version:
                self._teardown_tunnel_locked(cache_key)
            self._cache[cache_key] = _CacheEntry(resolved=resolved)

        return resolved

    def invalidate(self, alias: Optional[str] = None) -> None:
        """Invalida o cache. Sem alias, limpa tudo."""
        with self._lock:
            if alias:
                self._cache.pop(alias, None)
                self._teardown_tunnel_locked(alias)
            else:
                self._cache.clear()
                for key in list(self._tunnels.keys()):
                    self._teardown_tunnel_locked(key)

    def shutdown(self) -> None:
        """Fecha tuneis SSH abertos. Chame no shutdown do produto."""
        with self._lock:
            for key in list(self._tunnels.keys()):
                self._teardown_tunnel_locked(key)
            self._cache.clear()

    # ----------------------------------------------------------
    # Materializacao — psycopg2 / SQLAlchemy
    # ----------------------------------------------------------
    def get_psycopg2(self, alias: str, **kwargs: Any) -> Any:
        """Abre `psycopg2.connect(...)`. Ajusta host/porta para o forwarder
        local quando a conexao usa tunel SSH."""
        try:
            import psycopg2  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "psycopg2 nao instalado. `pip install psycopg2-binary`."
            ) from exc

        resolved = self.resolve(alias=alias)
        if not resolved:
            raise RuntimeError(f"Conexao '{alias}' nao encontrada ou sem permissao")

        host, port = self._materialize_host_port(alias, resolved)

        connect_kwargs: Dict[str, Any] = {
            "host": host,
            "port": port,
            "dbname": resolved.database_name,
            "user": resolved.username,
            "password": resolved.password,
            "connect_timeout": kwargs.pop("connect_timeout", 10),
        }
        if resolved.schema_name:
            options = kwargs.pop("options", None)
            opt_str = f"-c search_path={resolved.schema_name}"
            connect_kwargs["options"] = f"{opt_str} {options}" if options else opt_str
        connect_kwargs.update(kwargs)

        return psycopg2.connect(**connect_kwargs)

    def get_engine(self, alias: str, **engine_kwargs: Any) -> Any:
        """Cria `sqlalchemy.create_engine(...)` com pool curto. Cacheado
        nao — cada chamada cria engine novo (descarte com .dispose())."""
        try:
            from sqlalchemy import create_engine  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "SQLAlchemy nao instalado. `pip install sqlalchemy`."
            ) from exc

        resolved = self.resolve(alias=alias)
        if not resolved:
            raise RuntimeError(f"Conexao '{alias}' nao encontrada ou sem permissao")

        host, port = self._materialize_host_port(alias, resolved)
        # Reconstroi DSN com host/porta efetivos (pode ser do forwarder)
        scheme = resolved.dsn().split("://", 1)[0]
        user = quote_plus(resolved.username)
        pwd = quote_plus(resolved.password)
        dsn = f"{scheme}://{user}:{pwd}@{host}:{port}/{resolved.database_name}"

        engine_kwargs.setdefault("pool_pre_ping", True)
        engine_kwargs.setdefault("pool_size", 2)
        engine_kwargs.setdefault("max_overflow", 4)
        engine_kwargs.setdefault("pool_recycle", 1800)

        if resolved.engine == "postgresql" and resolved.schema_name:
            connect_args = engine_kwargs.setdefault("connect_args", {})
            connect_args.setdefault("options", f"-c search_path={resolved.schema_name}")

        return create_engine(dsn, **engine_kwargs)

    @contextmanager
    def get_session(self, alias: str, **engine_kwargs: Any) -> Iterator[Any]:
        """Context manager: cria engine + Session, fecha tudo no exit."""
        try:
            from sqlalchemy.orm import sessionmaker  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "SQLAlchemy nao instalado. `pip install sqlalchemy`."
            ) from exc

        engine = self.get_engine(alias, **engine_kwargs)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
            engine.dispose()

    # ----------------------------------------------------------
    # Internos — fetch e tunel
    # ----------------------------------------------------------
    def _fetch(
        self,
        alias: Optional[str],
        connection_id: Optional[str],
    ) -> Optional[ResolvedConnection]:
        """Chama `GET /api/database-connection/resolve` no AdminCenter."""
        if not getattr(self._admin, "config", None) or not self._admin.config.enabled:
            logger.debug("AdminCenter desabilitado — nao posso resolver conexao")
            return None

        params: Dict[str, str] = {}
        if alias:
            params["alias"] = alias
        elif connection_id:
            params["id"] = connection_id

        response = self._admin._make_request(
            "GET",
            "/database-connection/resolve",
            params=params,
        )
        # `"data" not in response` NAO basta: o AdminCenter responde a negativa
        # de acesso com o envelope completo e `data: None`. O guard antigo
        # deixava passar, `from_dict(None)` fazia `None.get(...)` e levantava
        # AttributeError — que o except abaixo nao capturava. O erro subia cru
        # ate o produto, onde virava "sem permissao" sem que ninguem tivesse
        # olhado o motivo real. Checar o VALOR resolve a raiz.
        dados = response.get("data") if isinstance(response, dict) else None
        if not dados:
            logger.warning(
                "Falha ao resolver conexao (alias=%s, id=%s): %s",
                alias,
                connection_id,
                # A mensagem do servidor e o unico lugar que diz POR QUE —
                # tipicamente "Acesso negado a esta conexao" (falta de
                # database_access para o principal desta api-key).
                (response or {}).get("message") or "resposta sem dados",
            )
            return None

        try:
            return ResolvedConnection.from_dict(dados)
        except (AttributeError, KeyError, ValueError, TypeError) as e:
            logger.error("Payload de /resolve invalido: %s", e)
            return None

    def _materialize_host_port(
        self,
        cache_key: str,
        resolved: ResolvedConnection,
    ) -> Tuple[str, int]:
        """Devolve host/porta efetivos. Abre tunel se necessario."""
        if not resolved.use_tunnel:
            return resolved.host, resolved.port

        if resolved.tunnel_type == "ssh":
            return self._open_ssh_tunnel(cache_key, resolved)

        if resolved.tunnel_type == "cloudflare":
            cfg = resolved.tunnel_config or {}
            local_host = cfg.get("local_host", "127.0.0.1")
            local_port = cfg.get("local_port")
            if not local_port:
                logger.warning(
                    "Cloudflare tunnel sem local_port — usando host original. "
                    "Configure `cloudflared access tcp` e preencha tunnel_config."
                )
                return resolved.host, resolved.port
            return local_host, int(local_port)

        return resolved.host, resolved.port

    def _open_ssh_tunnel(
        self,
        cache_key: str,
        resolved: ResolvedConnection,
    ) -> Tuple[str, int]:
        """Abre/reusa SSHTunnelForwarder e devolve (host, porta) locais."""
        try:
            from sshtunnel import SSHTunnelForwarder  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "sshtunnel nao instalado. `pip install sshtunnel`."
            ) from exc

        with self._lock:
            existing = self._tunnels.get(cache_key)
            if existing and existing.is_active:
                return "127.0.0.1", existing.local_bind_port

        cfg = resolved.tunnel_config or {}
        ssh_host = cfg.get("ssh_host")
        ssh_user = cfg.get("ssh_user")
        if not ssh_host or not ssh_user:
            raise ValueError(
                "tunnel_config invalido: ssh_host e ssh_user sao obrigatorios"
            )

        forwarder_kwargs: Dict[str, Any] = {
            "ssh_address_or_host": (ssh_host, int(cfg.get("ssh_port", 22))),
            "ssh_username": ssh_user,
            "remote_bind_address": (resolved.host, resolved.port),
            "set_keepalive": 30.0,
        }
        if cfg.get("ssh_password"):
            forwarder_kwargs["ssh_password"] = cfg["ssh_password"]
        if cfg.get("ssh_private_key"):
            forwarder_kwargs["ssh_pkey"] = cfg["ssh_private_key"]
            if cfg.get("ssh_private_key_password"):
                forwarder_kwargs["ssh_private_key_password"] = cfg["ssh_private_key_password"]

        tunnel = SSHTunnelForwarder(**forwarder_kwargs)
        tunnel.start()

        with self._lock:
            self._tunnels[cache_key] = tunnel

        return "127.0.0.1", tunnel.local_bind_port

    def _teardown_tunnel_locked(self, cache_key: str) -> None:
        """Deve ser chamado com self._lock segurado."""
        tunnel = self._tunnels.pop(cache_key, None)
        if tunnel is not None:
            try:
                tunnel.stop()
            except Exception as e:
                logger.warning("Falha ao parar SSH tunnel '%s': %s", cache_key, e)
