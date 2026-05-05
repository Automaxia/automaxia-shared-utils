# spec.md — automaxia-shared-utils

Especificação técnica da lib Python `automaxia_utils` (versão **1.5.0**).
Reflete o estado atual do código.

---

## 1. Objetivo

Biblioteca compartilhada entre os produtos Python da Automaxia que abstrai a
integração com o `admincenter-api`. Responsabilidades:

- Logging estruturado (aplicação / execução HTTP / processos de negócio).
- Cofre: leitura de secrets e environment variables centralizadas.
- Catálogo de prompts: `get_prompt`, `get_effective_prompt`, log de uso.
- Tracking de tokens com cálculo de custos (multi-provider).
- **JobRunner**: scheduler local + receiver HTTP de webhooks do painel.
- **Database connections broker** (1.5.0+): `resolve_connection`,
  `get_db_connection`, `get_db_engine`, `get_db_session` — credenciais
  decriptadas via `/database-connection/resolve`, com cache TTL,
  invalidação por `version` e túnel SSH/Cloudflare transparente.
- Auth middleware FastAPI para produtos que precisam validar JWT do AdminCenter.

Distribuição: Git+HTTPS (`pip install git+https://github.com/automaxia/automaxia-shared-utils.git`).

---

## 2. Stack

| Item | Valor |
|---|---|
| Python | 3.8+ (testado até 3.13) |
| HTTP | `requests`, `httpx` |
| Schedulers | APScheduler 3.10+ (lazy) |
| Cron | croniter 2.0+ (lazy) |
| Tokenização | tiktoken 0.7+, litellm 1.40+ |
| Server (jobs) | FastAPI minúsculo + `aiohttp` (porta 8001) |
| Auth | `python-jose` (HS256) — extra `[auth]` |
| Configs | `python-decouple`, `pydantic-settings` |

---

## 3. Estrutura de pastas

```
automaxia_utils/
├── __init__.py             # API pública re-exportada
├── admin_center/
│   ├── __init__.py
│   ├── service.py          # AdminCenterService (~970 linhas)
│   ├── jobs.py             # JobRunner (APScheduler + webhook server)
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
# AdminCenter
AdminCenterService, AdminCenterConfig
get_admin_center_service, reset_admin_center_service
AdminCenterContext, track_execution

# JobRunner
JobRunner

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
get_authenticated_user (alias get_current_user)
require_product_access
login_via_admincenter
```

Submódulos privados — uso direto não é garantido entre versões.

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
- JWT cacheado por 1h (`_token_lock`); renovado proativamente aos ~55min ou
  imediatamente em 401.
- Mode (`test` ou `live`) derivado do prefix da API key — propagado como
  claim no JWT e (fallback) no header `X-AdminCenter-Mode`.

### 5.4 Batch worker

Logs e usage de tokens entram numa `queue.Queue`. Worker thread daemon
desempilha e despacha em lote (`batch_size=50` ou `batch_interval=2s`).
`flush()` força drain síncrono — útil em testes e shutdown.

### 5.5 Helpers de uso

- `AdminCenterContext()`: context manager que chama `flush()` no exit.
- `@track_execution(process_name)`: decorator que envolve função em
  `log_process(started → completed/failed)` com `duration_ms`.

---

## 6. Database Connections broker (`admin_center/connections.py`)

### 6.1 Visão geral

Centraliza credenciais de banco no AdminCenter. Os produtos não guardam
mais host/porta/usuário/senha em `.env` local — chamam o resolver do
AdminCenterService que devolve credenciais decriptadas com TTL.

```python
from automaxia_utils import get_admin_center_service
admin = get_admin_center_service()

# Resolver puro — DTO com credenciais decriptadas
resolved = admin.resolve_connection(alias='casan_prod')

# psycopg2
conn = admin.get_db_connection('casan_prod')

# SQLAlchemy engine (descarte com .dispose())
engine = admin.get_db_engine('casan_prod')

# SQLAlchemy session (commit/rollback automático no exit)
with admin.get_db_session('casan_prod') as session:
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

---

## 7. JobRunner (`admin_center/jobs.py`)

### 6.1 Contrato esperado pelos produtos

```python
from automaxia_utils import JobRunner, get_admin_center_service

runner = JobRunner(get_admin_center_service())
runner.register("rpa_boletos.rodada", _rodada)
runner.register("rpa_boletos.relatorio", _relatorio)
runner.start(block=True)

# Dentro do handler:
runner.report_progress(percent=30, message="Baixando boletos")
```

### 6.2 Componentes

- **HTTP listener** (FastAPI): `0.0.0.0:8001`, rotas `POST /control` e
  `GET /healthz`. Validação HMAC-SHA256 do header `X-AdminCenter-Signature`
  contra `ADMIN_CENTER_JOBS_WEBHOOK_SECRET`.
- **Polling**: a cada 30s, `GET /api/agent/job?product_id=&environment_id=`.
  Reconcilia `APScheduler` local com config remota (cria/remove/reschedule
  por `config_version`); processa `force_run_at` como fallback de webhook.
- **APScheduler local**: cron resiliente — se AdminCenter cair, jobs
  continuam disparando.
- **Run lifecycle**:
  1. `POST /api/agent/job/{id}/run` → cria `process_execution_logs`,
     devolve `run_id`.
  2. Executa o handler em thread separada.
  3. `runner.report_progress(percent, message)` → `PATCH /api/agent/job/run/{run_id}/progress`.
  4. `POST /api/agent/job/run/{run_id}/finish` com `status='completed'|'failed'`.
- **HMAC do webhook**: `expected = HMAC-SHA256(body, secret).hex()` →
  `hmac.compare_digest`.

### 6.3 Eventos do `/control`

| Evento | Reação |
|---|---|
| `job.run_now` | despacha handler em thread, responde 200 imediato |
| `job.paused` | trigger sync de polling pra refrescar APScheduler |
| `job.resumed` | idem |

---

## 8. Token tracking (`token_tracking/counter.py`)

### 7.1 Hierarquia de contagem

```
1. response.usage (API)              → exato
2. LiteLLM token_counter()           → universal (100+ modelos)
3. APIs nativas (Anthropic native, Google usage_metadata)  → exato
4. tiktoken                          → fallback offline (OpenAI)
5. len(text) // 4                    → último recurso
```

### 7.2 Hierarquia de preços

```
1. LiteLLM cost_per_token            → mantido pela comunidade
2. AdminCenter API ai_models         → preços cadastrados no painel (cache)
3. Fallback hardcoded                → mar/2026
```

### 7.3 Funções principais

| Função | O que faz |
|---|---|
| `track_api_response(response, model, prompt_text, prompt_id)` | Detecta provider, registra tokens + custo via `track_token_usage` |
| `track_openai_call(...)` | Helper específico OpenAI |
| `estimate_tokens_and_cost(prompt, model, estimated_response_length)` | Estimativa prévia |
| `count_tokens_smart(text, model)` | Hierarquia automática |
| `count_tokens_litellm/tiktoken(text, model)` | Direto |
| `extract_tokens_from_response(response, model)` | Lê `usage` de qualquer provider |
| `HybridTokenCounter()` | Cache thread-safe reutilizável |
| `LangChainTokenCallback()` | Callback p/ LangChain chains |
| `invalidate_model_price_cache(model)` | Limpa preços cacheados |

### 7.4 Currency service

Cotação USD→BRL com cache de 30min. Fontes: env (`USD_BRL_RATE`) > cache >
API > fallback.

---

## 9. Auth middleware (`auth/middleware.py`)

Para produtos que sobem **outra** API que precisa validar JWT do
AdminCenter (caso típico: backend interno do produto que dá tela em cima
da automação).

### 8.1 Modos

- **LOCAL**: HS256 + `SECRET_KEY` compartilhada. Sem ida ao AdminCenter
  por request.
- **REMOTE**: chamada HTTP ao AdminCenter para validar token.

### 8.2 API

```python
from automaxia_utils import AdminCenterAuth, AdminCenterAuthConfig
from automaxia_utils import get_authenticated_user, require_product_access

cfg = AdminCenterAuthConfig.from_env()
auth = AdminCenterAuth(cfg)

@app.get("/me")
def me(user = Depends(get_authenticated_user)):
    return user

@app.get("/produto/{produto_id}")
def get(produto_id: str, _ = Depends(require_product_access(produto_id))):
    ...
```

### 8.3 Models

- `AuthenticatedUser` — `user_id`, `email`, `organization_id`,
  `product_access: List[ProductAccess]`.
- `ProductAccess` — `product_id`, `product_slug`, `profile_name`,
  `is_active`.

---

## 10. Variáveis de ambiente

Mínimas que um produto consumidor precisa ter:

```ini
ADMIN_CENTER_ENABLED=true
ADMIN_CENTER_URL=https://admincenter-api.automaxia.com.br/api
ADMIN_CENTER_URL_LOCAL=http://localhost:8002/api    # opcional, dev only
ADMIN_CENTER_API_KEY=sk_test_…
ADMIN_CENTER_ORGANIZATION_ID=…
ADMIN_CENTER_PRODUCT_ID=…
ADMIN_CENTER_ENVIRONMENT_ID=…

# Se for usar JobRunner:
ADMIN_CENTER_JOBS_WEBHOOK_PORT=8001
ADMIN_CENTER_JOBS_WEBHOOK_SECRET=<igual ao products.webhook_secret no painel>

# Se for usar AdminCenterAuth em modo LOCAL:
ADMIN_CENTER_AUTH_SECRET=<mesma SECRET_KEY do AdminCenter>
```

---

## 11. Versionamento

Histórico declarado no README:

| Versão | Marco |
|---|---|
| v1.0.0 (2025-01-15) | Base: tracking + AdminCenterService |
| v1.1.0 (2026-03-17) | Prompts centralizados + LiteLLM + Google Gemini |
| v1.4.0 | JobRunner + modo test\|live + APScheduler + croniter |
| v1.5.0 (atual, 2026-05-05) | Database connections broker (`ResolvedConnection`, `ConnectionResolver`, `resolve_connection`/`get_db_session`/`get_db_engine`/`get_db_connection` no `AdminCenterService`, túnel SSH/Cloudflare, extra `[database]`) |

Versão fica em `setup.py:__version__` e exportada em
`automaxia_utils.__version__`. Ainda **sem CHANGELOG.md separado** e **sem
tags Git formais** — versionamento depende do `setup.py` + commit hash
referenciado nos `requirements.txt` dos produtos.

---

## 12. Resiliência & threading

- Singleton com `_lock` em `get_admin_center_service()`.
- Worker thread daemon para fila de logs/usage.
- `_token_lock` para refresh do JWT.
- `_price_cache_lock` em `HybridTokenCounter`.
- `JobRunner` usa thread separada para o aiohttp loop e outra para o poll.
- Execução do handler de job é em thread (não bloqueia HTTP nem polling).
- **Falhas em rede nunca derrubam o produto consumidor** — log + skip.

---

## 13. Limitações conhecidas

- README `~500` linhas é a única documentação detalhada — falta diagrama
  de sequência (fluxo "Rodar agora", batch worker).
- Cobertura de testes parcial (unit). Falta integração end-to-end
  (run + report + finish).
- `JobRunner` não tem suíte de teste ainda (HMAC, polling, force_run_at).
- Token cache para `get_secret`/`get_variable` é simples (TTL implícito);
  invalidar exige `reset_admin_center_service()`.
- Sem versionamento explícito de endpoints (v1, v2). Mudanças no
  `admincenter-api` quebram clientes silenciosamente.
- `AdminCenterContext` chama `flush()` mas não `shutdown()` — worker
  segue vivo até o processo sair.
