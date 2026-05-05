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
- **Auth middleware**: `AdminCenterAuth` para FastAPI (JWT local ou remote)
  + `get_current_user` + `require_product_access`.

Versão atual: **1.5.0**.

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
├── setup.py                        # versão 1.4.0
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

- Polling: `GET /agent/job?product_id=…&environment_id=…` a cada 30s.
- HTTP listener: `POST /control` em `0.0.0.0:8001` (validação HMAC-SHA256).
- APScheduler local: roda crons mesmo se AdminCenter cair (resiliência).
- Lifecycle de run: `POST /agent/job/{id}/run` → executa handler →
  `POST /agent/job/run/{run_id}/finish`.
- Progresso: `runner.report_progress(percent, message)` → PATCH async.

---

## 6. API pública (`__init__.py`)

```python
from automaxia_utils import (
    # AdminCenter
    AdminCenterService, AdminCenterConfig,
    get_admin_center_service, reset_admin_center_service,
    AdminCenterContext, track_execution,
    # JobRunner
    JobRunner,
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
   (`docs/spec.md`) — endpoints `/agent/job/*`, `/auth/gerar-token/api-key`,
   `/secret`, `/prompt`, `/database-connection/resolve`.
3. **Adicionou novo provider**: estenda `track_api_response` em
   `token_tracking/counter.py` e cubra os campos extras na `extract_tokens_from_response`.
4. **Refatoração no JobRunner**: garanta que `register/start/shutdown` e
   `report_progress` continuam estáveis — produtos chamam isso direto.
5. **Publicação**: hoje é via Git tag. Commit em `main`, tag
   `v<major>.<minor>.<patch>`, atualize a referência sha no
   `requirements.txt` dos produtos (ou troque para tag oficial quando
   estabilizar).
6. **Sempre atualize `docs/spec.md` e `docs/tasks.md`** ao introduzir
   funcionalidade ou cobrir tarefa em aberto.

---

## 9. Pontos de atenção (gotchas)

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

- Especificação técnica: [docs/spec.md](docs/spec.md)
- Backlog: [docs/tasks.md](docs/tasks.md)
- README de uso: [README.md](README.md)
- AdminCenter (servidor): `../admincenter-api`
- Painel: `../admincenter-web`
