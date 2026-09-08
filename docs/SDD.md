# SDD — automaxia-shared-utils

> Desenho interno da lib `automaxia_utils` **1.15.1**.
> Requisitos: [SPEC.md](SPEC.md) · Backlog: [TASKS.md](TASKS.md) ·
> Histórico: [`../CHANGELOG.md`](../CHANGELOG.md)
> Contratos transversais: [`../../../docs/SDD.md`](../../../docs/SDD.md) — **não
> repetidos aqui** (JWT, cofre, auto-registro, silo `mode`).

Última revisão: **2026-09-08**.

> O desenho descrito aqui é o mesmo desde a 1.12.0; o que entrou depois foram
> capacidades novas sobre ele — a **linha do tempo de execução**
> (`agent_step`/`log_step`/`execution_scope`, 1.15.0, contrato em
> [`../../../docs/SDD.md`](../../../docs/SDD.md) §5.6) e, na **1.15.1**, o batch
> worker passando a inspecionar o **envelope** da resposta: o `admincenter-api`
> devolve recusa de escrita dentro de um HTTP `201`, e a lib contabilizava como
> enviada uma linha que nunca existiu. Detalhe em [`../CHANGELOG.md`](../CHANGELOG.md).

---

## 1. Princípio de desenho

A lib roda **dentro do processo do produto**. Duas consequências mandam em todo o
resto:

1. **Ela não pode derrubar o hospedeiro.** Import protegido, rede best-effort,
   worker daemon, exceção logada e engolida no caminho de telemetria.
2. **Ela não pode custar latência no caminho quente.** Tudo que é leitura do
   control plane tem cache; tudo que é escrita de telemetria é fila.

A exceção deliberada a (1) são as **barreiras de segurança**: gate de produto e
dependencies de permissão negam com 503 quando não conseguem decidir. Preferir
"não sei, então não" a "não sei, então sim" foi decisão explícita da 1.10.0.

---

## 2. Mapa de módulos

| Módulo | Papel | Depende de |
|---|---|---|
| `admin_center/service.py` | Fachada do control plane: config, JWT, fila de logs, prompts, tokens, variáveis, secrets | `requests` |
| `admin_center/connections.py` | Broker do cofre: `ResolvedConnection` + cache + túnel | lazy: `psycopg2`, `sqlalchemy`, `sshtunnel` |
| `admin_center/jobs.py` | `JobRunner`: WS + listener HMAC + APScheduler + run lifecycle | lazy: `apscheduler`, `croniter`, `aiohttp` |
| `auth/middleware.py` | Validação de JWT + RBAC + gate de produto | **FastAPI** (opcional) |
| `registration/client.py` | Manifest, `POST /product/register`, heartbeat daemon | `requests` |
| `migrations/runner.py` | `alembic upgrade` com retry | **alembic** (opcional) |
| `token_tracking/counter.py` | Contagem e preço multi-provider | lazy: `tiktoken`, `litellm` |

Nenhum módulo importa outro fora de `admin_center` ↔ `jobs` (run context) — é o
que mantém o pacote utilizável em pedaços.

---

## 3. Estado global e threading

Este é o ponto mais delicado da lib: ela mantém **estado de processo**.

| Estado | Onde | Proteção | TTL |
|---|---|---|---|
| Singleton do serviço | `get_admin_center_service()` | `_lock` | processo |
| JWT | `AdminCenterService` | `_token_lock` | ~55 min (renova proativo; imediato em 401) |
| Fila de telemetria | `queue.Queue` + worker daemon | thread-safe | lote de 50 ou 2 s |
| Conexões resolvidas | `ConnectionResolver` | lock interno | `expires_at` do servidor (~5 min) |
| Permissões do usuário | `_PERMISSION_CACHE` | — | **60 s** |
| Preço de modelo | `HybridTokenCounter` | `_price_cache_lock` | processo |
| Modelo efetivo por agente | serviço | — | processo (`invalidate_effective_model_cache`) |
| Run context (job) | thread-local | por thread | duração do run |

**Threads vivas num produto típico:** main (FastAPI) · batch worker · heartbeat ·
listener HTTP do JobRunner · loop WS · APScheduler · uma thread por run.

**Circuit breaker de auth** (`_open_auth_circuit`): sequência de falhas na troca
de API key por JWT abre o circuito e para de tentar por uma janela — sem isso, um
AdminCenter fora do ar vira tempestade de retry vinda de todos os produtos.

---

## 4. Fluxos

### 4.1 Boot de um satélite

```
1. load_dotenv()                      identidade de bootstrap
2. get_admin_center_service()         API key -> JWT (claims: org/product/env/mode)
3. get_variable()                     variáveis do produto -> os.environ
4. run_migrations()                   alembic upgrade head (retry)
5. register_with_platform(MANIFEST)   POST /product/register  [best-effort]
6. start_heartbeat_loop(MANIFEST)     thread daemon           [best-effort]
```

⚠️ A ordem 3→4 importa: quem lê `os.getenv` **no import** de um módulo precisa
que o passo 3 já tenha rodado. É o caso do Vision (ver o SDD dele) — e é por isso
que o `subir.bat` espera o `/health` do AdminCenter antes de abrir os satélites.

### 4.2 Request autenticada

```
Authorization: Bearer <JWT>
   └─ get_authenticated_user  ──HS256 local──▶ AuthenticatedUser
        └─ require_permission("x:read")
             ├─ claims já trazem permissões? usa
             └─ senão POST /auth/me/full (cache 60 s) ──▶ permite | 403 | 503
```

Custo em regime: **zero round-trip** — o JWT valida local e a permissão fica em
cache por 60 s.

### 4.3 Consulta ao banco do cliente

```
alias/connection_id ─▶ ConnectionResolver
      ├─ cache válido (não expirou, mesma `version`)? devolve
      └─ GET /database-connection/resolve
            ├─ use_tunnel? abre/reusa SSHTunnelForwarder (lazy import)
            └─ credenciais decifradas -> engine/session
```

Rotação de senha no painel muda `version`; a próxima chamada invalida o cache e
**fecha o túnel antigo**. Não é proativo: o produto só percebe na chamada
seguinte (ou em `force_refresh=True`).

### 4.4 Execução de job

```
painel "Rodar agora"
  ├─ WS  job.run_now (canal principal)
  └─ webhook HMAC POST :8001/control (fallback)
        └─ run_job(existing_run_id?)
             POST /agent/job/{id}/run → run_id
             thread do handler ── report_progress ──▶ PATCH …/progress
             fim → POST …/finish {completed|failed|cancelled}
```

`job.cancel_run` seta `cancel_event` no `_RunContext`; se o handler não cooperar
mas a flag ficou setada, o `run_job` reporta `cancelled` mesmo assim — o registro
reflete o que o operador pediu.

---

## 5. Decisões de design

| # | Decisão | Porquê |
|---|---|---|
| D-01 | Import de `auth`/`migrations` protegido por `try/except ImportError` | FastAPI e alembic não são dependência de RPA ou cliente puro; quebrar o pacote inteiro por isso seria inaceitável |
| D-02 | Telemetria em fila com worker daemon | log não pode entrar no caminho quente nem falhar a request |
| D-03 | Rede best-effort em registro/heartbeat/log | control plane fora do ar não pode impedir o produto de atender |
| D-04 | **Fail-closed** em gate de produto e permissão (1.10.0) | é barreira de segurança: "não consigo decidir" tem que negar |
| D-05 | Cache de permissão com TTL 60 s | mudança de perfil precisa propagar em janela humana sem custar round-trip por request |
| D-06 | `None` = "não declarado" no manifest | default `False` zerava, no re-registro, flags ajustadas na UI |
| D-07 | Modelo de IA resolvido pelo **agente**, não pelo `.env` | trocar modelo por etapa sem deploy; o mesmo valor alimenta o tracking, então o custo não mente |
| D-08 | Lazy import de `psycopg2`/`sqlalchemy`/`sshtunnel`/`tiktoken` | quem só resolve credencial não paga a dependência |
| D-09 | Invalidação de conexão por `version`, não por polling | rotação de senha sem restart, sem tráfego extra |
| D-10 | Heartbeat em **thread**, não em asyncio | mantém o módulo utilizável fora de FastAPI (Flask, Celery, CLI) |

---

## 6. Riscos estruturais

- **Estado de processo** — todo cache é local. Com N réplicas, N caches: mudança
  de role propaga em até 60 s **por réplica**; conexão invalidada idem.
- **`JobRunner` sem eleição de líder** — N réplicas disparam o mesmo cron.
  Contorno operacional: scheduler com `replicas: 1`.
- **Distribuição por `git+…@main` sem pin** — não há barreira que impeça um
  produto de subir com uma versão antiga da lib. O sintoma é sempre indireto
  (falta um símbolo, um campo não viaja); confira `automaxia_utils.__version__`
  **no pod**.
- **Contrato sem versão** — a lib fala com `/agent/*`, `/auth/*`, `/log/*`,
  `/prompt/*`, `/database-connection/*`, `/product/*`. Mudança incompatível
  nesses endpoints é uma quebra silenciosa.
- **`SECRET_KEY` única** — rotação exige troca simultânea em toda a frota.
