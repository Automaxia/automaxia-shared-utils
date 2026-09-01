# SPEC — automaxia-shared-utils (`automaxia_utils`)

> **Escopo:** o que a lib entrega e o contrato que ela expõe aos produtos.
> Desenho interno: [SDD.md](SDD.md) · Backlog: [TASKS.md](TASKS.md)
> Contratos transversais: [`../../../docs/SDD.md`](../../../docs/SDD.md) — o
> lado servidor destes contratos vive no
> [`admincenter-api`](../../admincenter-api/docs/SPEC.md).

**Versão da lib:** **1.15.0** (`setup.py:__version__`).
**Aderente ao `admincenter-api` até a migration `0044`.**
Última revisão: **2026-08-31**.

---

## 1. O que a lib é

É a **cola do ecossistema Studio**: tudo que um produto satélite precisa para
falar com o control plane sem reimplementar nada. Um produto novo instala a lib,
declara um manifest e ganha identidade, permissão, credencial, prompt, log e
faturamento.

| Capacidade | Símbolos |
|---|---|
| **Auth/RBAC** — validar o JWT do AdminCenter localmente e checar permissão | `get_authenticated_user`, `require_permission`, `require_any_permission`, `has_permission`, `has_any_permission`, `enrich_user_with_permissions`, `invalidate_permission_cache`, `require_product_access`, `login_via_admincenter` |
| **Auto-registro** — entrar no catálogo declarando o que o produto é | `ProductManifest`, `ProductRegistrationConfig`, `register_with_platform`, `send_heartbeat`, `start_heartbeat_loop` |
| **Cofre de conexões** — resolver credencial de cliente em runtime | `ConnectionResolver`, `ResolvedConnection` (+ `resolve_connection`/`get_db_*` no serviço) |
| **Serviço do control plane** — logs, secrets, variáveis, prompts, tokens | `AdminCenterService`, `AdminCenterConfig`, `get_admin_center_service`, `AdminCenterContext`, `track_execution` |
| **Jobs** — cron local + webhook/WS do painel, com cancelamento cooperativo | `JobRunner`, `JobCancelled` |
| **Token tracking** — contagem e custo multi-provider | `track_api_response`, `track_openai_call`, `count_tokens_smart`, `HybridTokenCounter`, `LangChainTokenCallback`, … |
| **Migrations** — `alembic upgrade` com retry no lifespan | `run_migrations` |

**Regra que organiza tudo:** *falha de rede nunca derruba o produto consumidor.*
Registro, heartbeat, log e tracking são best-effort. As **exceções deliberadas**
são as barreiras de segurança (gate de produto e dependencies de permissão), que
são **fail-closed** desde a 1.10.0 — ver §9.4.

---

## 2. Requisitos

| # | Requisito |
|---|---|
| RF-01 | Trocar API key por JWT e manter o token renovado sem o produto saber |
| RF-02 | Validar JWT do AdminCenter **localmente** (sem round-trip por request) |
| RF-03 | Resolver permissões efetivas do usuário e expor dependencies FastAPI |
| RF-04 | Registrar o produto no catálogo a partir de um manifest declarativo + heartbeat |
| RF-05 | Resolver credencial de banco do cliente por alias/`connection_id`, com cache e túnel |
| RF-06 | Ler variáveis de ambiente, secrets e prompts do AdminCenter |
| RF-07 | Enviar logs (aplicação/execução/processo) em lote assíncrono |
| RF-08 | Contabilizar tokens e custo por produto, agente e prompt |
| RF-09 | Executar jobs agendados pelo painel (cron local, webhook HMAC, WebSocket) |
| RF-10 | Aplicar migrations do produto no startup com retry tolerante a banco indisponível |

| # | Requisito não-funcional |
|---|---|
| RNF-01 | Import do pacote **não pode quebrar** por dependência opcional ausente (FastAPI, alembic, psycopg2) |
| RNF-02 | Nenhuma chamada de rede no caminho quente do produto sem cache |
| RNF-03 | Thread-safe: singleton, fila de logs, cache de token e de preços |
| RNF-04 | Python 3.8+ (rodando em 3.13 no ecossistema) |

---

## 3. Estrutura

```
automaxia_utils/
├── __init__.py             # API pública (imports protegidos por try/except)
├── admin_center/
│   ├── service.py          # AdminCenterService
│   ├── jobs.py             # JobRunner (APScheduler + listener HTTP + WS)
│   └── connections.py      # ResolvedConnection + ConnectionResolver
├── auth/
│   └── middleware.py       # AdminCenterAuth + helpers RBAC
├── registration/
│   └── client.py           # ProductManifest + register/heartbeat
├── migrations/
│   └── runner.py           # run_migrations (alembic com retry)
├── token_tracking/
│   └── counter.py          # contagem + preço multi-provider
└── config/settings.py
```

### 3.1 Imports opcionais — a razão dos `try/except`

`__init__.py` protege dois blocos:

- **`migrations`** — `alembic` só é dependência de quem tem banco próprio. RPAs e
  clientes puros não instalam.
- **`auth`** — depende de **FastAPI**, que só faz sentido em quem expõe HTTP.

Sem os guards, o import do pacote inteiro quebraria num RPA sem FastAPI.
`__all__` é montado condicionalmente (`_AUTH_AVAILABLE` / `_MIGRATIONS_AVAILABLE`).

> ⚠️ Efeito colateral a conhecer: se o FastAPI estiver instalado mas o módulo
> `auth` tiver um erro de import legítimo, o sintoma é `ImportError: cannot
> import name 'require_permission'` no produto — não o erro real. Ao investigar,
> importe `automaxia_utils.auth.middleware` direto para ver a exceção verdadeira.

---

## 4. API pública

```python
# AdminCenter
AdminCenterService, AdminCenterConfig
get_admin_center_service, reset_admin_center_service
AdminCenterContext, track_execution

# Jobs
JobRunner, JobCancelled

# Cofre de conexões
ResolvedConnection, ConnectionResolver

# Auto-registro (1.10.0+)
ProductManifest, ProductRegistrationConfig
register_with_platform, send_heartbeat, start_heartbeat_loop

# Migrations (opcional — requer alembic)
run_migrations

# Token tracking
track_api_response, track_openai_call, estimate_tokens_and_cost
count_tokens_tiktoken, count_tokens_litellm, count_tokens_smart
extract_tokens_from_response
HybridTokenCounter, LangChainTokenCallback, invalidate_model_price_cache

# Auth (opcional — requer FastAPI)
AdminCenterAuth, AdminCenterAuthConfig, AuthenticatedUser
get_authenticated_user (alias de get_current_user), require_product_access
login_via_admincenter
has_permission, has_any_permission, enrich_user_with_permissions      # 1.11.0+
require_permission, require_any_permission, invalidate_permission_cache
```

Submódulos privados não têm garantia de estabilidade entre versões.

---

## 5. AdminCenterService

### 5.1 Configuração (`AdminCenterConfig.from_env`)

| Campo | Default | Env |
|---|---|---|
| `api_url` | — | `ADMIN_CENTER_URL` (em `ENVIRONMENT=development`, prioriza `ADMIN_CENTER_URL_LOCAL`/`ADMIN_CENTER_DEV_URL`) |
| `api_key` | — | `ADMIN_CENTER_API_KEY` (em dev, aceita `ADMIN_CENTER_API_KEY_test`) |
| `organization_id` / `product_id` / `environment_id` | — | opcionais desde 1.7.0 — ver abaixo |
| `enabled` | `true` | `ADMIN_CENTER_ENABLED` |
| `batch_mode` / `batch_size` / `batch_interval` | `true` / 50 / 2 | `ADMIN_CENTER_BATCH_*` |
| `timeout` / `max_retries` | 10 / 2 | `ADMIN_CENTER_TIMEOUT` / `_MAX_RETRIES` |

`is_valid()` exige apenas **`api_url` + `api_key`**. Com API key escopada
(migration 0022 do AdminCenter), o JWT devolve `organization_id`/`product_id`/
`environment_id` e a lib se auto-preenche em `_get_access_token`. Valor
explícito no `.env` **vence** o escopo da chave.

### 5.2 Métodos

| Método | Endpoint | Modo |
|---|---|---|
| `get_variable(environment_id?)` | `GET /environment/{env}/variables` | sync, cache |
| `get_secret(name)` | `GET /secret?name=` | sync |
| `resolve_connection(alias=, connection_id=, force_refresh=)` | `GET /database-connection/resolve` | sync, cache TTL |
| `get_db_connection` / `get_db_engine` / `get_db_session` | (via `/resolve`) | sync, lazy import |
| `invalidate_connection_cache(alias?)` | — | local |
| `get_prompt(slug)` · `get_prompt_by_id(id)` · `get_prompts(...)` | `GET /prompt*` | sync, cache |
| `get_effective_prompt(agent_slug, product_id?)` | `GET /prompt/effective-prompt` | sync |
| `log_prompt_usage(...)` | `POST /prompt-usage-log` | fila |
| `log_application(...)` · `log_execution(...)` · `log_process(...)` | `POST /log/*` | fila |
| `agent_step(agent_slug, label=)` · `execution_scope()` · `log_step(...)` | `POST /logs/step` | fila, **em lote** |
| `get_application_logs(...)` | `GET /logs/application` | sync |
| `track_token_usage(...)` | `POST /token-usage/` | fila |
| `invalidate_model_cache` · `invalidate_effective_model_cache` | — | local |
| `flush()` / `shutdown()` | — | drena fila / fecha túneis |

### 5.3 O modelo de IA é do AGENTE, não do `.env`

`get_effective_prompt(agent_slug)` devolve, além do conteúdo, o **modelo
efetivo**: `model_id`, `model_name`, `is_model_overridden`. A resolução é
**override do produto → padrão do agente** (`product_agents.model_id` vence
`agents.model_id`).

Corolário operacional que já custou tempo: **mudar o modelo na tela de Agentes
não tem efeito** se existir override no vínculo agente × produto. Mude no
vínculo.

`track_token_usage` fecha o ciclo: passando só `agent_slug` (sem `model_name`),
a lib resolve o mesmo modelo do effective-prompt — o registro de custo não
diverge do que de fato rodou.

**Dimensões de custo por agente** (1.11.0, colunas da migration 0036):
`agent_id` = a **etapa** executora (roteador, gerador de SQL, sumarizador);
`area_agent_id` = a **área de negócio** dona da requisição. Ambos opcionais e
omitidos do payload quando `None`. Sem eles, o custo das etapas desaparece
dentro do agente de negócio.

### 5.4 Logs em lote

Fila `queue.Queue` + worker daemon; despacha por `batch_size=50` ou
`batch_interval=2s`. `flush()` drena de forma síncrona.

`log_application` aceita campos top-level que batem 1:1 com colunas indexáveis
de `application_logs` (`logger_name`, `module_name`, `function_name`,
`line_number`, `exception_*`, `stack_trace`); `context` vira `extra_data`
(JSONB) consultável por `extra_data_filter`.

Quando emitido **de dentro de um handler de job**, `run_id` + `job_slug` entram
automaticamente em `extra_data` (thread-local `current_run_context()` em
`jobs.py`) — dá para filtrar log por execução sem depender de substring.

Pelo mesmo mecanismo, `log_process()` e `@track_execution` herdam o `job_id` do
run (1.9.0) — é o que permite ao faturamento vincular execução ao job e aplicar
a cobrança de mensagens Meta, que é opt-in por job.

---

## 6. Cofre de conexões

```python
admin = get_admin_center_service()
resolved = admin.resolve_connection(alias='base_do_cliente')   # DTO decifrado
engine   = admin.get_db_engine('base_do_cliente')              # SQLAlchemy
with admin.get_db_session('base_do_cliente') as s:             # commit/rollback
    s.execute(text('SELECT 1'))
```

- **Contrato:** `GET /database-connection/resolve?alias=…` ou `?id=<uuid>`, com o
  mesmo JWT do serviço. Resposta = `ResolvedConnection` (id, alias, engine, host,
  port, database_name, schema_name, username/password **decifrados**, use_tunnel,
  tunnel_type, tunnel_config, access_level, allowed_schemas/tables,
  denied_statements, `version`, `expires_at`).
- **Cache** por alias no processo, conferindo `is_expired()`. `version` diferente
  invalida a entrada e **fecha o túnel SSH antigo** — é isso que permite
  rotacionar senha sem reiniciar o produto.
- **Túnel SSH** (`sshtunnel`, lazy) ou **Cloudflare Access** (aponta para o
  `cloudflared` local; subir/derrubar é do operador).
- **Lazy imports:** `psycopg2`/`sqlalchemy`/`sshtunnel` só entram quando usados —
  `pip install "automaxia-utils[database]"`.

---

## 7. Auto-registro de produtos (1.10.0+)

```python
MANIFEST = ProductManifest(
    code='meu-produto', name='Meu Produto', version='1.0.0',
    organization_slug='automaxia',          # só na PRIMEIRA criação
    base_url=..., frontend_url=...,
    product_type='web_app',                 # ENUM do AdminCenter
    requires_connection=True,
    requires_connection_engines=['postgresql'],
    permissions=[{'key': 'meu:read', 'label': 'Ver'}],
    menus=[{'key': 'x', 'label': 'X', 'route': '/x', 'requires': ['meu:read']}],
)
register_with_platform(MANIFEST)
start_heartbeat_loop(MANIFEST)
```

**O que o registro sobrescreve** (todo boot chama `POST /product/register`):

| Fonte | Campos |
|---|---|
| **Cadastro** (a tela vence) | `name`, `description`, `slug`, `status`, `visible_on_home` |
| **Manifest** (todo deploy sobrescreve) | `version`, `base_url`, `frontend_url`, `product_type`, `health_path`, `contract_version`, `requires_connection`, `requires_connection_engines` + **replace-all das permissões** |

**`None` significa "não declarado"** — `to_payload()` remove chaves nulas para
não zerar no catálogo o que o produto não declarou. É deliberado em
`requires_instance`/`requires_connection`: com default `False`, o re-registro
disparado por heartbeat zerava a flag ajustada na UI.

**Restrições do contrato** (divergência devolve 422 e o produto **não aparece**):
- `product_type` ∈ `web_app | api | mobile_app | desktop_app | bot`.
  `internal_tool` **não existe** aqui.
- `menus[].requires` é **lista**, não string.

> ⚠️ **1.12.0 — `requires_connection_engines`.** Os manifests do Vision e do
> Turing já declaravam o campo antes do dataclass aceitá-lo. O `TypeError`
> resultante era engolido pelo `except Exception` do manifest do Turing, que
> ficava com `PRODUCT_MANIFEST = None`: **o produto simplesmente nunca se
> registrava, em silêncio.** Se um satélite "não aparece no catálogo e não
> loga erro", suspeite primeiro de construção de manifest.

---

## 8. JobRunner

```python
runner = JobRunner(get_admin_center_service())
runner.register("meu_produto.rodada", handler)
runner.start(block=True)                 # webhook é o canal principal
runner.report_progress(30, "Baixando…")
runner.raise_if_cancelled()              # em loops longos
```

- **Canais:** WebSocket `/agent/job/ws` (principal) · listener HTTP local
  `:8001 POST /control` com HMAC-SHA256 · polling `GET /agent/job` **opt-in**
  (`start(with_polling=True)`) para ambientes sem ingresso.
- **APScheduler local** mantém o cron rodando mesmo com o AdminCenter fora.
  Job sem `cron_expression` é manual-only (não entra no scheduler).
- **Lifecycle:** `POST /agent/job/{id}/run` → `PATCH …/progress` → `POST …/finish`.
  Quando o webhook traz `run_id` (fluxo `trigger-with-attachment`), o runner
  **reutiliza** o run em vez de criar um paralelo órfão em 0%.
- **Cancelamento cooperativo:** evento `job.cancel_run` seta o `cancel_event` do
  run; `is_cancelled()`/`raise_if_cancelled()` levantam `JobCancelled`, que o
  `run_job` converte em `status='cancelled'`.

---

## 9. Auth e RBAC

### 9.1 Modos

- **LOCAL** — HS256 com a `SECRET_KEY` compartilhada. Sem ida ao AdminCenter por
  request. É o modo usado no Studio.
- **REMOTE** — valida chamando o AdminCenter.

### 9.2 O `sub` do AdminCenter é o E-MAIL

O id do usuário vem no claim `user_id` (UUID). Produto que faz `int(sub)`
levanta `ValueError` — que **não é `JWTError`** e escapa do `except` óbvio,
mandando o usuário logado de volta para a tela de login.

### 9.3 Helpers de permissão (1.11.0)

```python
@app.get("/relatorio")
def relatorio(_ = Depends(require_permission("meu:read"))):
    ...
```

- Resolução via **`POST /auth/me/full`** do AdminCenter, com **cache TTL 60s** por
  `user_id` (`invalidate_permission_cache(user_id?)` limpa).
- `organization_permissions` valem para qualquer produto;
  `products[].permissions` são escopadas por `product_slug`.
- Super admin (`system:full_access`) bypassa qualquer check.
- `AuthenticatedUser.permission_detail` guarda o bloco estruturado.

### 9.4 Fail-closed é o default (1.10.0)

O gate de produto e as dependencies de permissão **negam com 503** quando não
conseguem resolver (5xx, timeout, conexão). `AUTH_PRODUCT_GATE_FAIL_OPEN=true`
restaura o comportamento antigo — use só para destravar incidente, nunca como
configuração permanente. Envelope HTTP 200 com `{success:false, status_code:403}`
conta como **negado**.

---

## 10. Migrations (`run_migrations`)

`alembic upgrade <target>` com retry tolerante a banco ainda subindo — feito para
o lifespan dos backends.

| Env | Default | Papel |
|---|---|---|
| `ALEMBIC_CONFIG_PATH` | `alembic.ini` | caminho do ini |
| `ALEMBIC_RETRY_MAX` | 30 | tentativas |
| `ALEMBIC_RETRY_DELAY` | 2 | segundos entre tentativas |

`alembic.ini` ausente → `FileNotFoundError` (falha explícita, não silenciosa).

> ⚠️ **Não** coloque `?options=-csearch_path%3D…` na `DATABASE_URL`: o Alembic lê
> a URL com `ConfigParser`, que trata `%` como interpolação. Defina o
> `search_path` **no role** (ver [`../../../docs/SDD.md`](../../../docs/SDD.md) §4.1).

---

## 11. Variáveis de ambiente do consumidor

```ini
# Identidade de bootstrap — o mínimo
ADMIN_CENTER_ENABLED=true
ADMIN_CENTER_URL=https://admincenter-api.automaxia.com.br/api
ADMIN_CENTER_URL_LOCAL=http://127.0.0.1:8002/api     # dev
ADMIN_CENTER_API_KEY=sk_live_…
ADMIN_CENTER_PRODUCT_SLUG=meu-produto

# Opcionais desde 1.7.0 (a API key escopada resolve)
ADMIN_CENTER_ORGANIZATION_ID= / _PRODUCT_ID= / _ENVIRONMENT_ID=

# Auto-registro
PRODUCT_REGISTRATION_KEY=<mesma chave do AdminCenter>
PRODUCT_VERSION= / PRODUCT_BASE_URL= / PRODUCT_FRONTEND_URL=
PRODUCT_AUTO_REGISTER=false        # em dev: --reload re-registra a cada save
PRODUCT_HEARTBEAT_INTERVAL_SECONDS=60

# JobRunner
ADMIN_CENTER_JOBS_WEBHOOK_PORT=8001
ADMIN_CENTER_JOBS_WEBHOOK_SECRET=<products.webhook_secret>

# Auth
ADMIN_CENTER_AUTH_SECRET=<mesma SECRET_KEY do AdminCenter>
AUTH_CACHE_TTL=300
AUTH_PRODUCT_GATE_FAIL_OPEN=false  # só para destravar incidente
```

---

## 12. Histórico de versões

| Versão | Marco |
|---|---|
| v1.0.0 | Base: tracking + AdminCenterService |
| v1.1.0 | Prompts centralizados + LiteLLM + Gemini |
| v1.4.0 | JobRunner + modo `test`/`live` |
| v1.5.0 | Broker de conexões (`ConnectionResolver`, túnel SSH/Cloudflare) |
| v1.7.0 | Cancelamento cooperativo; `existing_run_id`; jobs manual-only; webhook como canal default; auto-população de escopo via API key; logs estruturados |
| v1.8.0 | `ADMIN_CENTER_API_KEY_test` em `ENVIRONMENT=development` |
| v1.9.0 | `log_process` herda `job_id` do run (faturamento por job) |
| v1.10.0 | **Módulo `registration`** (manifest + heartbeat); gate de produto **fail-closed** |
| v1.11.0 | **Helpers RBAC** via `/auth/me/full` (cache 60s, fail-closed); `agent_id`/`area_agent_id` no token tracking |
| v1.12.0 | Consolidação no Studio: `registration` **rastreado no git** (antes o módulo inteiro estava untracked — quem instalava por `@main` recebia 1.8.0 sem `ProductManifest`), `requires_connection_engines` no manifest, `run_migrations`, modelo do agente no effective-prompt e `track_token_usage(agent_slug=…)`. Tag `v1.12.0`. |

| v1.13.0 | `fastapi` e `python-jose` declarados como extras (CI da lib passa) |
| v1.14.0 *(2026-08-26)* | Sincronização com a `infrabalance-shared-utils` 2.9.0: gate de produto voltou a funcionar (`ADMIN_CENTER_PRODUCT_SLUG`), auth em dev deixa de bater em produção, `ResolvedConnection` cobre `rest`/`arcgis`/`databricks` e os campos da migration 0042, `log_min_level`, desmembramento do `context` do log, guardas de `has_logging_identity()`, descoberta de `product_id`/`environment_id` por slug, `log_process(execution_id=…)`. Detalhes em [`../CHANGELOG.md`](../CHANGELOG.md). |

Distribuição: `pip install git+https://github.com/Automaxia/automaxia-shared-utils.git`.
| **v1.15.0** *(atual, 2026-08-31)* | **Execução observável**: `agent_step`/`execution_scope`/`log_step` gravam a linha do tempo de dentro de uma execução (`execution_steps`, migration 0044), com o modelo EFETIVO por etapa; `log_process` ganha `agent_slug`/`area_agent_slug`/`connection_id` (migration 0043) resolvendo pelo mesmo cache do token tracking; passos vão num POST só. Detalhes em [`../CHANGELOG.md`](../CHANGELOG.md). |

Sem PyPI. A partir da 1.14.0 o histórico canônico é o
[`CHANGELOG.md`](../CHANGELOG.md); esta tabela fica como índice das versões.

---

## 13. Limitações conhecidas

- **Congelamento no Docker:** consumidores instalam por `git+…@main` sem pin e a
  camada do `pip` é chaveada pelo `requirements.txt`, que nunca muda. Com
  `cache-from: type=gha` a lib congela na versão do dia em que a camada nasceu.
  Use `no-cache-filters` no estágio de deps e confira a versão **no pod**.
- **Sem versionamento de endpoint** — mudança de contrato no `admincenter-api`
  quebra a lib em silêncio. Mitigação proposta: header `X-Lib-Version`.
- **`JobRunner` é single-process** — várias réplicas disparam o mesmo cron.
  Deploy do scheduler com `replicas: 1`.
- **`get_secret` sem TTL** — só invalida em `reset_admin_center_service()`.
- **`AdminCenterContext` chama `flush()`, não `shutdown()`** — o worker segue vivo
  até o processo sair; em CLI efêmera atrasa o exit em até `batch_interval`.
- **`AdminCenterAuth` LOCAL** assume a mesma `SECRET_KEY` em todo o ecossistema;
  rotação é big-bang (sem `kid`/JWKS).
- **Cobertura de teste parcial** — o `JobRunner` e o broker de conexões não têm
  suíte própria.
