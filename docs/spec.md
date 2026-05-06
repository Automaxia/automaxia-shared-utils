# spec.md — automaxia-shared-utils (infrabalance)

Especificação técnica da lib Python `automaxia_utils` (versão **1.4.0**).
Reflete o estado atual do código.

---

## 1. Objetivo

Biblioteca compartilhada entre os produtos Python da Infrabalance/Linedata
que abstrai a integração com o `plataforma-backend`. Responsabilidades:

- Logging estruturado (aplicação / execução HTTP / processos de negócio).
- Cofre: leitura de secrets e environment variables centralizadas.
- Catálogo de prompts: `get_prompt`, `get_effective_prompt`, log de uso.
- Tracking de tokens com cálculo de custos (multi-provider).
- **Database connections broker** (1.4.0+): `resolve_connection`,
  `get_db_connection`, `get_db_engine`, `get_db_session` — credenciais
  decriptadas via `/database-connection/resolve`, com cache TTL,
  invalidação por `version` e túnel SSH/Cloudflare transparente.
- Auth middleware FastAPI para produtos que precisam validar JWT.

Distribuição: Git+HTTPS.

---

## 2. Stack

| Item | Valor |
|---|---|
| Python | 3.8+ (testado até 3.13) |
| HTTP | `requests`, `httpx` |
| Tokenização | tiktoken 0.7+, litellm 1.40+ |
| Auth | `python-jose` (HS256) — extra `[auth]` |
| Database broker | psycopg2-binary, sqlalchemy, sshtunnel — extra `[database]` |
| Configs | `python-decouple`, `pydantic-settings` |

---

## 3. Estrutura de pastas

```
automaxia_utils/
├── __init__.py             # API pública re-exportada
├── admin_center/
│   ├── __init__.py
│   ├── service.py          # AdminCenterService
│   └── connections.py      # ResolvedConnection + ConnectionResolver (broker DB)
├── auth/
│   ├── __init__.py
│   └── middleware.py       # AdminCenterAuth, get_current_user, …
├── token_tracking/
│   ├── __init__.py
│   └── counter.py          # HybridTokenCounter, LangChainTokenCallback, …
└── config/
    ├── __init__.py
    └── settings.py         # placeholder
```

---

## 4. API pública (`__init__.py`)

```python
# Plataforma
AdminCenterService, AdminCenterConfig
get_admin_center_service, reset_admin_center_service
AdminCenterContext, track_execution

# Database connections (broker centralizado)
ResolvedConnection, ConnectionResolver

# Token tracking
track_api_response, track_openai_call
estimate_tokens_and_cost
count_tokens_tiktoken, count_tokens_litellm, count_tokens_smart
extract_tokens_from_response
HybridTokenCounter, LangChainTokenCallback
invalidate_model_price_cache

# Auth middleware
AdminCenterAuth, AdminCenterAuthConfig
get_authenticated_user, require_product_access
login_via_admincenter
```

---

## 5. AdminCenterService

### 5.1 Configuração (`AdminCenterConfig`)

| Campo | Default | Origem env |
|---|---|---|
| `api_url` | — | `ADMIN_CENTER_URL` (ou `ADMIN_CENTER_URL_LOCAL` se `ENVIRONMENT=development`) |
| `api_key` | — | `ADMIN_CENTER_API_KEY` |
| `organization_id` | — | `ADMIN_CENTER_ORGANIZATION_ID` |
| `product_id` | — | `ADMIN_CENTER_PRODUCT_ID` |
| `environment_id` | — | `ADMIN_CENTER_ENVIRONMENT_ID` |
| `enabled` | true | `ADMIN_CENTER_ENABLED` |
| `batch_mode` | true | `ADMIN_CENTER_BATCH_MODE` |
| `batch_size` | 50 | `ADMIN_CENTER_BATCH_SIZE` |
| `batch_interval` | 2 | `ADMIN_CENTER_BATCH_INTERVAL` |
| `timeout` | 10 | `ADMIN_CENTER_TIMEOUT` |
| `max_retries` | 2 | `ADMIN_CENTER_MAX_RETRIES` |

### 5.2 Métodos principais

| Método | Backend endpoint | Async? |
|---|---|---|
| `log_application(level, message, context)` | `POST /api/log/application` | sim (fila) |
| `log_execution(endpoint, method, status_code, response_time_ms)` | `POST /api/log/execution` | sim (fila) |
| `log_process(process_name, status, duration_ms, ...)` | `POST /api/log/process` | sim (fila) |
| `get_secret(name)` | `GET /api/secret?name=` | sync |
| `get_variable(environment_id?)` | `GET /api/environment/{env}/variables` | sync, cache |
| `track_token_usage(model, prompt_tokens, completion_tokens, ...)` | `POST /api/token-usage/log` | sim (fila) |
| `get_prompt(slug)` | `GET /api/prompt?slug=` | sync, cache |
| `get_prompts(tags, is_active)` | `GET /api/prompt` | sync |
| `get_effective_prompt(agent_slug, product_id)` | `GET /api/prompt/effective-prompt` | sync |
| `log_prompt_usage(prompt_id, variables, final_prompt, tokens, model)` | `POST /api/prompt-usage-log` | sim (fila) |
| `resolve_connection(alias=, connection_id=, force_refresh=)` | `GET /api/database-connection/resolve` | sync, cache TTL |
| `get_db_connection(alias)` | (usa `/resolve`) | sync — `psycopg2.connection` |
| `get_db_engine(alias, **kwargs)` | (usa `/resolve`) | sync — `sqlalchemy.Engine` |
| `get_db_session(alias)` | (usa `/resolve`) | context manager |
| `invalidate_connection_cache(alias?)` | — | local |
| `invalidate_model_cache(model)` | — | local |
| `flush()` / `shutdown()` | — | drena fila + fecha túneis SSH |

### 5.3 Authentication

- Singleton thread-safe via `get_admin_center_service()`.
- API key trocada por JWT em `POST /api/auth/gerar-token/api-key`
  (header `api-key: sk_test_*` / `sk_live_*`).
- JWT cacheado por 1h (`_token_lock`); renovado proativamente aos ~55min
  ou imediatamente em 401.

### 5.4 Batch worker

Logs e usage de tokens entram numa `queue.Queue`. Worker thread daemon
desempilha e despacha em lote (`batch_size=50` ou `batch_interval=2s`).
`flush()` força drain síncrono — útil em testes e shutdown.

---

## 6. Database Connections broker (`admin_center/connections.py`)

### 6.1 Visão geral

Centraliza credenciais de banco na plataforma. Os produtos não guardam
mais host/porta/usuário/senha em `.env` local — chamam o resolver do
AdminCenterService que devolve credenciais decriptadas com TTL.

```python
from automaxia_utils import get_admin_center_service
admin = get_admin_center_service()

# Resolver puro — DTO com credenciais decriptadas
resolved = admin.resolve_connection(alias='cliente_prod')

# psycopg2
conn = admin.get_db_connection('cliente_prod')

# SQLAlchemy engine (descarte com .dispose())
engine = admin.get_db_engine('cliente_prod')

# SQLAlchemy session (commit/rollback automático no exit)
with admin.get_db_session('cliente_prod') as session:
    session.execute(text('SELECT 1'))
```

### 6.2 Contrato de rede

| Item | Valor |
|---|---|
| Endpoint | `GET /api/database-connection/resolve?alias=...` ou `?id=<uuid>` |
| Auth | mesmo JWT do AdminCenterService (humano OU api-key) |
| Resposta | `ResolvedConnection` envelope `{success, data, ...}` |
| TTL | `expires_at` no payload (default 5 min do servidor) |

`ResolvedConnection` (campos):

```
id, alias, engine, host, port, database_name, schema_name,
username, password,                         # decriptados
use_tunnel, tunnel_type, tunnel_config,
access_level, allowed_schemas, allowed_tables, denied_statements,
version, expires_at
```

### 6.3 Cache + invalidação por version

`ConnectionResolver` mantém um dict `alias -> ResolvedConnection` em
memória do processo. Cada chamada confere `is_expired()` antes de usar
o cacheado. Se `expires_at` passou, refaz o `/resolve`.

Quando o servidor retorna `version` diferente do que está em cache, o
resolver invalida a entrada antiga e fecha o túnel SSH dela (se houver) —
isso permite **rotação de senha sem reiniciar o produto**.

`admin.invalidate_connection_cache(alias)` força refresh manual.
`force_refresh=True` em `resolve_connection` ignora cache pontualmente.

### 6.4 Túnel SSH

Quando `use_tunnel=true` e `tunnel_type='ssh'`, o resolver:

1. Importa `sshtunnel.SSHTunnelForwarder` (lazy).
2. Abre forwarder com `tunnel_config = {ssh_host, ssh_port, ssh_user,
   ssh_password|ssh_private_key, ssh_private_key_password}`.
3. Aponta o engine/conexão para `127.0.0.1:<porta_local>` do forwarder.
4. Reusa o forwarder enquanto o cache estiver válido.
5. Fecha tudo no `admin.shutdown()`.

### 6.5 Cloudflare Access

Para `tunnel_type='cloudflare'`, o resolver assume que um
`cloudflared access tcp --hostname X --url 127.0.0.1:Y` está rodando no
host do produto. `tunnel_config = {local_host, local_port, api_url?,
api_key?}` — o resolver simplesmente aponta o engine para
`local_host:local_port`. Subir/derrubar o cloudflared é responsabilidade
do operador (systemd, k8s sidecar, etc).

### 6.6 Lazy imports

Apenas o módulo `connections.py` é carregado no startup. Os imports
caros (`psycopg2`, `sqlalchemy`, `sshtunnel`) só acontecem quando
`get_db_connection`, `get_db_engine`, `get_db_session` ou um túnel SSH
é necessário. Produtos que usam só `resolve_connection()` (e abrem a
conexão por conta própria) não precisam dessas deps.

Instalação opcional:

```
pip install "automaxia-utils[database]"   # psycopg2-binary + sqlalchemy + sshtunnel
```

### 6.7 Integração com produtos consumidores (padrão alias)

Padrão atual de consumo nos produtos `datachatai-backend` e
`dashboard-backend` (e demais que vierem):

#### Antes (≤ 1.3.0)

Produto guardava credenciais brutas em variáveis de ambiente:

```
DB_HOST=…    DB_PORT=…    DB_NAME=…
DB_USER=…    DB_PASSWORD=…    DB_SCHEMA=…
```

Senha trafegava no `.env` ou em `EnvironmentVariables` do AdminCenter.
Rotação exigia reiniciar o produto.

#### Hoje (1.4.0+)

Produto guarda apenas o **alias** da conexão cadastrada na plataforma:

```
DB_CONNECTION_ALIAS=cliente_prod
METRICA_DB_CONNECTION_ALIAS=metricas_centralizado    # apenas datachatai
```

A senha mora cifrada (AES-256-GCM) em `database_connections`. A lib
faz `GET /api/database-connection/resolve?alias=...`, recebe credenciais
decriptadas com TTL de 5 min e cacheia. Rotação no painel bumpa
`version` → próxima `resolve()` pega credencial nova **sem reiniciar**.

#### Caminhos de consumo

**Adapter legado** — produto mantém `Config.db_host`/`db_password`/etc;
só muda a fonte dos valores:

```python
# Em src/core/config.py do produto:
if self.db_connection_alias:
    resolved = resolve_connection_via_broker(self.db_connection_alias)
    if resolved:
        merge_resolved_into_config_dict(config_dict, resolved, prefix='db_')

self.db_host = config_dict.get('db_host')      # vem do broker
self.db_password = config_dict.get('db_password')   # decriptado
# DatabaseManager segue idêntico:
psycopg2.connect(self.config.db_connection)
```

Vantagem: zero impacto no resto do código.
Limitação: `psycopg2.connect(string)` direto **não ativa túnel SSH**.

**Direto** — produto chama os métodos novos e ganha túnel transparente:

```python
admin = get_admin_center_service()
conn = admin.get_db_connection('cliente_prod')   # SSH abre se cadastrado
with admin.get_db_session('cliente_prod') as session:
    session.execute(text("SELECT 1"))
```

#### Helpers locais nos produtos

Para o adapter, cada produto tem um helper:

| Produto | Arquivo | Funções |
|---|---|---|
| dashboard-backend | `core/connection_resolver.py` | `resolve_db_config_via_broker(alias)`, `to_db_config_dict(resolved)` |
| datachatai-backend | `src/core/connection_resolver.py` | `resolve_connection_via_broker(alias)`, `merge_resolved_into_config_dict(cfg, resolved, prefix='db_')` |

Ambos retornam `None` em falha (broker offline, alias inexistente,
sem permissão) e o produto cai pro fluxo legado (`DB_HOST`/`DB_USER`
do `.env`). Backward-compat 100%.

#### Autorização

No painel, dentro da conexão, conceder acesso ao produto:

| Campo | Valor |
|---|---|
| principal_type | `product` |
| principal_id | UUID do produto |
| access_level | `read` / `write` / `admin` |

Sem registro em `database_access`, `/resolve` retorna 403 e loga em
`database_access_logs`.

---

## 7. Token tracking (`token_tracking/counter.py`)

Hierarquia de contagem:

```
1. response.usage (API)             → exato
2. LiteLLM token_counter()          → universal
3. APIs nativas (Anthropic/Google)  → exato (extra opcional)
4. tiktoken                         → fallback offline (OpenAI)
5. len(text) // 4                   → último recurso
```

Hierarquia de preços:

```
1. LiteLLM cost_per_token       → pricing comunitário atualizado
2. Plataforma API               → preços cadastrados no painel (cache)
3. Fallback hardcoded           → mar/2026
```

`HybridTokenCounter`: thread-safe, com cache de preço (`_price_cache_lock`)
e cotação USD/BRL atualizada a cada 30min.

---

## 8. Auth middleware (`auth/middleware.py`)

`AdminCenterAuth` em dois modos:

- **LOCAL** (HS256 + SECRET_KEY compartilhada com a plataforma): valida
  JWT localmente sem round-trip. Mais rápido.
- **REMOTE** (chama a plataforma): valida em cada request via
  `POST /api/auth/validate`. Mais seguro mas adiciona latência.

`get_authenticated_user`, `require_product_access` e
`login_via_admincenter` cobrem o fluxo completo de auth para FastAPI.

Requer `pip install "automaxia-utils[auth]"` (puxa fastapi, python-jose).

---

## 9. Variáveis de ambiente

Mínimo no produto consumidor:

```
ADMIN_CENTER_ENABLED=true
ADMIN_CENTER_URL=https://plataforma-api.linedata.com.br/api
ADMIN_CENTER_API_KEY=sk_test_…
ADMIN_CENTER_ORGANIZATION_ID=…
ADMIN_CENTER_PRODUCT_ID=…
ADMIN_CENTER_ENVIRONMENT_ID=…
```

---

## 10. Versionamento

| Versão | Marco |
|---|---|
| v1.0.0 | Base: tracking + AdminCenterService |
| v1.1.0 | Prompts centralizados + LiteLLM |
| v1.4.0 (atual, 2026-05-05) | Database connections broker (`ResolvedConnection`, `ConnectionResolver`, `resolve_connection`/`get_db_session`/`get_db_engine`/`get_db_connection` no `AdminCenterService`, túnel SSH/Cloudflare, extra `[database]`) |

Versão fica em `setup.py:__version__` e exportada em
`automaxia_utils.__version__`.

---

## 11. Resiliência & threading

- Singleton com `_lock` em `get_admin_center_service()`.
- Worker thread daemon para fila de logs/usage.
- `_token_lock` para refresh do JWT.
- `_price_cache_lock` em `HybridTokenCounter`.
- `_lock` no `ConnectionResolver` (cache + tuneis).
- **Falhas em rede nunca derrubam o produto consumidor** — log + skip.

---

## 12. Limitações conhecidas

- **Sem versionamento de endpoints** — quando o `plataforma-backend`
  mudar contrato, a lib quebra silenciosamente. Mitigar com header
  `X-Lib-Version` + checagem no backend.
- **`get_secret` cache** não tem TTL — invalida só em
  `reset_admin_center_service()`.
- **`ConnectionResolver` cache TTL = 5min** — herdado do `expires_at`
  do servidor. Em runs longos com rotação de senha, vale chamar
  `admin.invalidate_connection_cache(alias)` antes ou usar
  `force_refresh=True`. A invalidação automática por `version` cobre o
  caso comum, mas só dispara na próxima chamada — não é proativa.
- **Auth LOCAL** assume mesma `SECRET_KEY` em produto e plataforma. Em
  produção, prefira REMOTE.
