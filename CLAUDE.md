# claud.md — automaxia-shared-utils

Manual de bordo da IA para a lib Python compartilhada que liga produtos da
Automaxia ao AdminCenter. Leia antes de mexer.

---

## 1. O que é

`automaxia-utils` (pacote `automaxia_utils`) — biblioteca Python instalada
nos produtos finais (RPAs, APIs, integrações) para falar com o AdminCenter
e fornecer infraestrutura compartilhada:

- **AdminCenter integration**: logs, secrets, environment variables, prompts
  centralizados, tracking de tokens/custos.
- **Database connections broker**: resolve credenciais de banco cadastradas no
  AdminCenter (`/database-connection/resolve`), abre conexões psycopg2 ou
  SQLAlchemy `Engine`/`Session`, gerencia túnel SSH/Cloudflare e cacheia por TTL
  com invalidação por `version`.
- **Job runner local**: APScheduler + servidor HTTP (porta 8001) que recebe
  webhooks do painel ("Rodar agora", pause, resume) e reporta progresso/finish.
- **Token tracking multi-provider**: contagem hierárquica (response.usage →
  LiteLLM → tiktoken → fallback) e cálculo de custos com cotação USD/BRL.
- **Auth + RBAC**: `AdminCenterAuth` para FastAPI (JWT local ou remote),
  `get_authenticated_user`, `require_product_access` e — desde a 1.11 — os
  helpers de permissão (`require_permission`, `has_permission`,
  `enrich_user_with_permissions`) resolvidos por `POST /auth/me/full`
  com cache de 60s e comportamento **fail-closed**.
- **Auto-registro de produtos** (1.10+): `ProductManifest`,
  `register_with_platform`, `start_heartbeat_loop` — é assim que um satélite
  entra no catálogo do AdminCenter.
- **Migrations**: `run_migrations` (alembic com retry) para o lifespan.

Versão atual: **1.14.0** — histórico em [CHANGELOG.md](CHANGELOG.md).

---

## 2. Stack

| Camada | Tecnologia |
|---|---|
| Linguagem | Python 3.8+ (compatível até 3.13) |
| HTTP | requests, httpx |
| Schedulers | APScheduler 3.10+ (lazy import) |
| Cron parser | croniter 2.0+ (lazy import) |
| HTTP server (jobs) | FastAPI minúsculo (porta 8001) — usado só pelo `JobRunner` |
| Token counters | tiktoken, litellm |
| Configs | python-decouple, pydantic-settings |
| Auth | python-jose (HS256) — `[auth]` extra |
| Distribuição | Git+HTTPS (sem PyPI ainda) |

Extras opcionais:

```bash
pip install "automaxia-utils[all]"          # tudo
pip install "automaxia-utils[langchain]"    # LangChain callback
pip install "automaxia-utils[providers]"    # Anthropic + Google nativos
pip install "automaxia-utils[database]"     # psycopg2, SQLAlchemy, sshtunnel
pip install "automaxia-utils[dev]"          # pytest, black, flake8, mypy, twine
```

---

## 3. Estrutura

```
automaxia-shared-utils/
├── README.md                       # ~500 linhas com quickstart + exemplos
├── setup.py                        # versão 1.14.0
├── requirements.txt
├── automaxia_utils/
│   ├── __init__.py                 # API pública re-exportada
│   ├── admin_center/
│   │   ├── __init__.py
│   │   ├── service.py              # AdminCenterService (~970 linhas)
│   │   ├── jobs.py                 # JobRunner (APScheduler + webhook server)
│   │   └── connections.py          # ResolvedConnection + ConnectionResolver (broker DB)
│   ├── auth/
│   │   ├── __init__.py
│   │   └── middleware.py           # AdminCenterAuth, get_current_user, …
│   ├── registration/
│   │   ├── __init__.py
│   │   └── client.py               # ProductManifest, register_with_platform, heartbeat
│   ├── migrations/
│   │   ├── __init__.py
│   │   └── runner.py               # run_migrations (alembic com retry)
│   ├── token_tracking/
│   │   ├── __init__.py
│   │   └── counter.py              # HybridTokenCounter, LangChainTokenCallback, …
│   └── config/
│       ├── __init__.py
│       └── settings.py             # placeholder mínimo
└── tests/                          # cobertura parcial (unit)
```

---

## 4. Como rodar

```powershell
# Instalar a partir do repo local (editable)
pip install -e .

# Ou direto do GitHub
pip install git+https://github.com/automaxia/automaxia-shared-utils.git

# Executar testes
pip install -e ".[dev]"
pytest -q
```

A lib é **stateful** via singleton: chame
`get_admin_center_service()` uma vez e reuse. Em testes, use
`reset_admin_center_service()` para isolar.

`.env` mínimo nos produtos consumidores:

```
ADMIN_CENTER_ENABLED=true
ADMIN_CENTER_URL=https://admincenter-api.automaxia.com.br/api
ADMIN_CENTER_API_KEY=sk_test_…
ADMIN_CENTER_ORGANIZATION_ID=…
ADMIN_CENTER_PRODUCT_ID=…
ADMIN_CENTER_ENVIRONMENT_ID=…
ADMIN_CENTER_JOBS_WEBHOOK_PORT=8001
ADMIN_CENTER_JOBS_WEBHOOK_SECRET=…    # mesmo valor de products.webhook_secret no painel
```

---

## 5. Padrões de arquitetura

### 5.1 Singleton + lazy

`get_admin_center_service()` cria uma única instância. Imports caros
(APScheduler, croniter, litellm) só acontecem quando o método que os usa é
chamado. Útil para apps que não precisam de scheduler.

### 5.2 Batch worker assíncrono

Logs (`log_application`, `log_execution`, `log_process`) e usage de tokens
(`track_token_usage`) entram numa fila e um worker thread despacha em lote.
Configurável via `AdminCenterConfig.batch_size` e `batch_interval`.

### 5.2.1 Correlação com o job (run context) — faturamento

Quando emitidos **de dentro de um handler de job** (executado pelo `JobRunner`),
`log_application` injeta `run_id` + `job_slug` no `extra_data`, e `log_process`
(logo `@track_execution` também) herda o **`job_id`** do run context via
`current_run_context()` (thread-local em `jobs.py`, import lazy p/ evitar
circular). Introduzido na **1.9.0**.

Efeito no AdminCenter: cada `process_execution_logs` grava `job_id`, o que
permite ao faturamento vincular a execução ao job e aplicar a **cobrança de
mensagens Meta (WhatsApp), que é opt-in por job**. Para cobrar, o RPA registra a
quantidade enviada em `output_data` (`enviados` | `sent` | `messages_sent`).
`log_process(..., job_id=...)` também aceita override explícito.

### 5.3 Auth: API key → JWT

A lib só envia API key crua para `POST /auth/gerar-token/api-key`. Demais
chamadas usam o JWT obtido (validade 1h, renovado automaticamente em 401
ou ~5min antes da expiração). O claim `mode` (test|live) é derivado do
prefix da API key (`sk_test_*` / `sk_live_*`).

### 5.4 Hierarquia de contagem de tokens

```
1. response.usage (API)             → exato
2. LiteLLM token_counter()          → universal
3. APIs nativas (Anthropic/Google)  → exato (extra opcional)
4. tiktoken                         → fallback offline (OpenAI)
5. len(text) // 4                   → último recurso
```

### 5.5 Hierarquia de preços

```
1. LiteLLM cost_per_token       → pricing comunitário atualizado
2. AdminCenter API              → preços cadastrados no painel (cache)
3. Fallback hardcoded           → mar/2026
```

### 5.6 ConnectionResolver (broker de banco)

`AdminCenterService.resolve_connection(alias=...)` chama
`GET /api/database-connection/resolve` no AdminCenter e devolve um
`ResolvedConnection` com host, port, database, **username/password
decriptados em memória** + `expires_at` (TTL).

Cache local por alias guardado em memória do processo. Quando o backend
retorna `version` diferente do cacheado, a entrada é invalidada — produtos
pegam credenciais novas sem precisar reiniciar.

Materialização:

```
admin.get_db_connection(alias)   → psycopg2.connection
admin.get_db_engine(alias)        → sqlalchemy.Engine
with admin.get_db_session(alias) as session:   # commit/rollback automático
    session.execute(text('select 1'))
```

Túnel SSH/Cloudflare é transparente:

- **SSH** (`tunnel_type='ssh'`): o resolver abre `sshtunnel.SSHTunnelForwarder`
  na primeira chamada, reusa o forwarder enquanto o cache estiver válido,
  e fecha tudo no `admin.shutdown()`. `tunnel_config` esperado:
  `{ssh_host, ssh_port, ssh_user, ssh_password|ssh_private_key}`.
- **Cloudflare Access** (`tunnel_type='cloudflare'`): assume-se que um
  `cloudflared access tcp --url 127.0.0.1:<porta>` esteja rodando no host
  do produto. `tunnel_config` traz `local_host/local_port` para apontar
  o engine para o cloudflared.

Lazy imports: `psycopg2`, `sqlalchemy`, `sshtunnel` só são importados quando
o método correspondente é chamado. Produtos que apenas chamam
`resolve_connection()` (e abrem a conexão por conta própria) não precisam
dessas deps — daí o extra opcional `[database]`.

### 5.7 JobRunner

- **WebSocket `/api/agent/job/ws` é o canal principal**; o HTTP listener
  (`POST /control` em `0.0.0.0:8001`, HMAC-SHA256) é o caminho de webhook.
- **Polling é opt-in desde a 1.7.0** (`start(with_polling=True)`) — use só
  quando o agente não puder expor ingresso (NAT/firewall).
- APScheduler local: roda crons mesmo se AdminCenter cair (resiliência).
- Lifecycle de run: `POST /agent/job/{id}/run` → executa handler →
  `POST /agent/job/run/{run_id}/finish`.
- Progresso: `runner.report_progress(percent, message)` → PATCH async.

### 5.8 Effective-prompt: agente + prompt + modelo

`AdminCenterService.get_effective_prompt(agent_slug, product_id=None)` resolve,
**numa chamada**, o prompt do agente **e o modelo de IA** para o produto:

- Prompt: `generic_content`, `generic_prompts`, `generic_temperature`,
  `generic_max_tokens`, `custom_content`, `is_customized`, `selected_prompt_ids`…
- **Modelo** (campos adicionados): `model_id`, `model_name`,
  `model_display_name`, `is_model_overridden` — modelo **efetivo** (override do
  produto → `agents.model_id`). O modelo mora no **agente**, não no prompt:
  `get_prompt(slug)` **não** traz modelo.

`track_token_usage` aceita `agent_slug` (e `model_name` virou **opcional**): sem
`model_name`, resolve o modelo do agente via effective-prompt (cacheado em
`_effective_model_cache`) — deixa `agents.model_id` autoritativo config→billing.
`invalidate_effective_model_cache(agent_slug=None)` limpa o cache após trocar o
modelo no painel. Assinatura retrocompatível (defaults nos positionais).

Padrão de consumo (o produto usa o modelo do agente na inferência e no billing):

```python
ep = admin.get_effective_prompt('sql-analyst')
system_message = ep['generic_content']
modelo = ep.get('model_name')          # modelo configurado no agente
# ... chama o LLM com `modelo` ...
admin.track_token_usage(agent_slug='sql-analyst',
                        prompt_tokens=pt, completion_tokens=ct)
```

> O `model_name` só chega no effective-prompt depois que o **admincenter-api**
> estiver com o código novo (campos de modelo no `get_effective_prompt`) e o
> agente tiver `model_id` configurado. Antes disso vem `None` → o produto cai no
> modelo do `.env` (fallback). Consumidores já com wiring: `talk`
> (automático) e o Vision (opt-in via `DASHBOARD_AGENT_SLUG`).

---

## 6. API pública (`__init__.py`)

```python
from automaxia_utils import (
    # AdminCenter
    AdminCenterService, AdminCenterConfig,
    get_admin_center_service, reset_admin_center_service,
    AdminCenterContext, track_execution,
    # JobRunner
    JobRunner, JobCancelled,
    # Auto-registro de produtos (1.10+)
    ProductManifest, ProductRegistrationConfig,
    register_with_platform, send_heartbeat, start_heartbeat_loop,
    # Migrations (opcional — requer alembic)
    run_migrations,
    # Database connections (broker centralizado)
    ResolvedConnection, ConnectionResolver,
    # Token tracking
    track_api_response, track_openai_call,
    estimate_tokens_and_cost,
    count_tokens_tiktoken, count_tokens_litellm, count_tokens_smart,
    extract_tokens_from_response,
    HybridTokenCounter, LangChainTokenCallback,
    invalidate_model_price_cache,
    # Auth
    AdminCenterAuth, AdminCenterAuthConfig,
    get_authenticated_user, require_product_access,
    login_via_admincenter,
    # RBAC (1.11+)
    require_permission, require_any_permission,
    has_permission, has_any_permission,
    enrich_user_with_permissions, invalidate_permission_cache,
)
```

Mantenha esta lista em sincronia com a documentação ao adicionar exports
novos. Quem consumir de submódulos (`from automaxia_utils.admin_center.service
import …`) é considerado privado e pode quebrar entre versões.

---

## 7. Estilo de código

- **Type hints obrigatórios** em assinaturas públicas. Use
  `from __future__ import annotations`.
- **Docstrings curtas** focando no porquê e nos efeitos colaterais (worker,
  IO de rede, threading).
- **Sem `print`**: use `logging.getLogger(__name__)`.
- **Lazy imports** em deps pesadas (APScheduler, litellm, croniter,
  tiktoken). Evite quebrar startup quando o produto não usa o subsistema.
- **Threadsafe**: serviços longos guardam `threading.Lock` quando mexem em
  cache (`_token_lock`, `_price_cache_lock`).
- **Resiliência**: toda chamada externa precisa de try/except + log; nunca
  derruba o produto consumidor.
- **Nomes**: módulos e funções em inglês; variáveis de domínio podem ficar
  em português (`organizacao_id` é evitado — preferimos `organization_id`).

---

## 8. Workflow esperado

1. **Antes de mudar API pública**: bumpa versão (semver) em `setup.py` e
   atualiza CHANGELOG no README.
2. **Mudou contrato com AdminCenter**: alinhe com `admincenter-api`
   (`docs/SPEC.md`) — endpoints `/agent/job/*`, `/auth/gerar-token/api-key`,
   `/secret`, `/prompt`, `/database-connection/resolve`.
3. **Adicionou novo provider**: estenda `track_api_response` em
   `token_tracking/counter.py` e cubra os campos extras na `extract_tokens_from_response`.
4. **Refatoração no JobRunner**: garanta que `register/start/shutdown` e
   `report_progress` continuam estáveis — produtos chamam isso direto.
5. **Publicação**: hoje é via Git tag. Commit em `main`, tag
   `v<major>.<minor>.<patch>`, atualize a referência sha no
   `requirements.txt` dos produtos (ou troque para tag oficial quando
   estabilizar).
6. **Sempre atualize `docs/SPEC.md` e `docs/TASKS.md`** ao introduzir
   funcionalidade ou cobrir tarefa em aberto.

---

## 9. Pontos de atenção (gotchas)

- **Imports opcionais**: `auth` depende de FastAPI e `migrations` de alembic —
  os dois entram por `try/except ImportError` no `__init__.py`. Sintoma de erro
  real dentro do módulo: `ImportError: cannot import name 'require_permission'`.
  Importe `automaxia_utils.auth.middleware` direto para ver a exceção verdadeira.
- **Gate de produto e permissão são FAIL-CLOSED** (1.10+): não conseguir decidir
  → 503. `AUTH_PRODUCT_GATE_FAIL_OPEN=true` só para destravar incidente.
- **`None` no manifest = "não declarado"**: `to_payload()` remove chaves nulas
  para não zerar no catálogo o que o produto não declarou. Um `TypeError` na
  construção do manifest engolido por `except Exception` deixa
  `PRODUCT_MANIFEST=None` e o produto **nunca se registra, em silêncio**.

- **JWT expira em 1h**: o método `_ensure_token()` deve ser chamado antes
  de qualquer request autenticada — não cacheie o header `Authorization`
  além do necessário.
- **Cotação USD/BRL**: cache em memória de 30min. Em testes, force valor
  via env var `USD_BRL_RATE` para evitar chamadas externas.
- **JobRunner em desktop Windows**: `aiohttp` precisa de proactor loop;
  funciona out-of-the-box, mas se algum produto rodar em Jython/PyPy
  pode quebrar.
- **HMAC mismatch**: o secret tem que ser **exatamente** o mesmo do painel
  (`products.webhook_secret`). Se foi regenerado, o `.env` precisa ser
  atualizado e o produto reiniciado.
- **Singleton + multi-process**: cada processo tem seu próprio singleton.
  Em deploys com múltiplos workers, cada um abre sua conexão e roda seu
  scheduler — em produção use `block=True` apenas no processo dedicado a
  schedulering.
- **Connection cache TTL**: o `ConnectionResolver` cacheia o resultado de
  `/resolve` por `expires_at` (default 5 min). Se você rotacionar a senha
  no painel, o produto só vai pegar a nova senha após o TTL — ou após
  `admin.invalidate_connection_cache(alias)` ou `force_refresh=True`. O
  backend bumpa `version` em cada mudança, e quando vier `version` novo
  o cache é trocado e o túnel SSH antigo é derrubado.
- **Tunnel SSH em multi-process**: cada processo abre seu próprio
  forwarder. Não é problema funcional, mas faz N conexões SSH ao bastion.
  Se virar gargalo, use Cloudflare Access TCP no host (1 cloudflared para
  todos os processos) e configure `tunnel_type='cloudflare'` no painel.
- **Lazy deps de banco**: `psycopg2`, `sqlalchemy` e `sshtunnel` não vêm
  por default. Produtos que usam `get_db_*` devem instalar via extra
  `pip install "automaxia-utils[database]"`.
- **Billing do cliente NÃO usa o custo gravado pela lib**: o
  `estimated_cost` + `metadata.cost_brl/vlr_dolar` que o `track_token_usage`
  grava (hierarquia LiteLLM → API → fallback) é referência **interna** de
  custo do provedor. A fatura de IA é calculada no admincenter-api por
  `token_usage.model_id` × custos cadastrados em `ai_models` — portanto o
  que importa para o faturamento é (1) reportar o **modelo correto**
  (`model_name`/`agent_slug`) no tracking e (2) manter os preços dos modelos
  sincronizados no painel (sync LiteLLM). Mensagens Meta: contadas de
  `output_data.enviados|sent|messages_sent` das execuções de jobs com
  `bill_meta_messages=true`, ao preço de `product_jobs.meta_price_per_message`
  (fallback: tabela por categoria).

---

## 10. Comandos rápidos

```powershell
# Bump local + reinstalar nos produtos
pip install -e .
pip install --force-reinstall git+https://github.com/automaxia/automaxia-shared-utils.git

# Smoke test do JobRunner local
python -c "from automaxia_utils import JobRunner, get_admin_center_service; \
            r = JobRunner(get_admin_center_service()); \
            r.register('test.echo', lambda: print('hello')); \
            r.start(block=True)"

# Lint + type check + testes
black automaxia_utils tests
flake8 automaxia_utils
mypy automaxia_utils
pytest -q
```

---

## 11. Onde encontrar mais

- Especificação técnica: [docs/SPEC.md](docs/SPEC.md)
- **Desenho interno: [docs/SDD.md](docs/SDD.md)**
- Backlog: [docs/TASKS.md](docs/TASKS.md)
- Transversal do ecossistema: [../../docs/SDD.md](../../docs/SDD.md)
- README de uso: [README.md](README.md)
- AdminCenter (servidor): `../admincenter-api`
- Painel: `../admincenter-web`
