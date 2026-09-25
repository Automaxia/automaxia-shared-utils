# TASKS — automaxia-shared-utils

> Requisitos: [SPEC.md](SPEC.md) · Desenho: [SDD.md](SDD.md)
> Backlog transversal: [`../../../docs/TASKS.md`](../../../docs/TASKS.md)
> **Convenção de ID:** itens abertos usam `LIB-##`. Itens concluídos ficam como
> checklist histórica, sem ID.

Última revisão: **2026-09-21** · lib **1.18.0**.

> O backlog abaixo foi escrito na 1.12.0 e continua válido — o que entrou depois
> (1.13.0 → 1.15.1) está no [`../CHANGELOG.md`](../CHANGELOG.md), que é o
> histórico canônico desde a 1.14.0.
>
> **A 1.15.1 foi publicada em 08/09/2026** (`e3c847f`, tag `v1.15.1`), fechando o
> `LIB-06`. Com ela fora, o item que resta na frente é o **`LIB-03`**
> (pin + `no-cache-filters`): publicar põe a correção ao alcance do build, mas
> não a põe no pod enquanto a camada do pip continuar congelada.

---

## ✅ Done

### 1.10.0 → 1.12.0 (ago/2026 — integração ao Studio)
- [x] **Módulo `registration`**: `ProductManifest`, `ProductRegistrationConfig`,
      `register_with_platform`, `send_heartbeat`, `start_heartbeat_loop`.
- [x] **Gate de produto fail-closed** por default (503 quando não dá para
      decidir); `AUTH_PRODUCT_GATE_FAIL_OPEN=true` reverte.
- [x] **Helpers RBAC** (`require_permission`, `require_any_permission`,
      `has_permission`, `has_any_permission`, `enrich_user_with_permissions`,
      `invalidate_permission_cache`) via `POST /auth/me/full`, cache TTL 60s.
- [x] **Dimensões de custo por agente** em `track_token_usage`
      (`agent_id` = etapa, `area_agent_id` = área de negócio).
- [x] **Modelo do agente no `get_effective_prompt`** (`model_id`/`model_name`/
      `is_model_overridden`) + `track_token_usage(agent_slug=…)` resolvendo o
      mesmo modelo — o registro de custo deixa de divergir do que rodou.
- [x] **`run_migrations`** (alembic com retry) para o lifespan dos backends.
- [x] **`requires_connection_engines`** no manifest. Antes disso o campo já era
      declarado pelos manifests do Vision e do Harvest: o `TypeError` era engolido
      pelo `except Exception` do manifest do Harvest, que ficava
      `PRODUCT_MANIFEST=None` — **o produto nunca se registrava, em silêncio**.
- [x] **`registration/` passou a ser rastreado no git.** O módulo inteiro estava
      untracked: quem instalava por `@main` recebia 1.8.0 sem `ProductManifest`.
- [x] Tag `v1.12.0` publicada em `main`.

### Histórico anterior

### Núcleo
- [x] `AdminCenterConfig` com `from_env()` e settings via `.env`.
- [x] Singleton thread-safe (`get_admin_center_service`, `reset_admin_center_service`).
- [x] Exchange API key → JWT em `/auth/gerar-token/api-key`.
- [x] Refresh proativo do JWT (~55min) e em 401.
- [x] Mode `test|live` derivado do prefix da API key, propagado como claim
      e header `X-AdminCenter-Mode`.

### Logging
- [x] `log_application(level, message, context)`.
- [x] `log_execution(endpoint, method, status_code, response_time_ms)`.
- [x] `log_process(process_name, status, duration_ms, metadata)`.
- [x] Batch worker assíncrono (queue + thread) com `batch_size` e
      `batch_interval` configuráveis.
- [x] `flush()` síncrono para drain manual.
- [x] `AdminCenterContext` (context manager).
- [x] `@track_execution` (decorator com lifecycle started/completed/failed).
- [x] `log_process` herda `job_id` do run context (1.9.0) — vincula execução
      ao job p/ faturamento (cobrança Meta opt-in por job). Aceita `job_id` explícito.

### Cofre & catálogo
- [x] `get_secret(name)` (sem expor encriptados).
- [x] `get_variable(environment_id)` com cache.
- [x] `get_prompt(slug)`, `get_prompts(tags, is_active)`.
- [x] `get_effective_prompt(agent_slug, product_id)` — resolução
      customizado → selecionado → genérico.
- [x] `log_prompt_usage(prompt_id, variables, final_prompt, tokens, model)`.

### Token tracking
- [x] Hierarquia de contagem (response.usage → LiteLLM → APIs nativas →
      tiktoken → fallback).
- [x] Hierarquia de preços (LiteLLM → AdminCenter cache → fallback hardcoded).
- [x] `track_api_response`, `track_openai_call`, `estimate_tokens_and_cost`.
- [x] `HybridTokenCounter` thread-safe.
- [x] `LangChainTokenCallback`.
- [x] Cotação USD/BRL com cache de 30min.
- [x] Detecção automática de provider (OpenAI/Anthropic/Google) por
      assinatura do `response`.

### JobRunner
- [x] `register(slug, handler)`, `start(block)`, `shutdown()`.
- [x] HTTP listener `aiohttp` em `0.0.0.0:8001` com `POST /control` e
      `GET /healthz`.
- [x] Validação HMAC-SHA256 (`X-AdminCenter-Signature`) constant-time.
- [x] Polling de 30s contra `/agent/job` com reconcile de APScheduler local.
- [x] Detecção de `force_run_at` (fallback de webhook).
- [x] Lifecycle de run (start → progress → finish) integrado.
- [x] `report_progress(percent, message, step_name)` com run-id local-thread.
- [x] APScheduler local resiliente — jobs disparam mesmo se AdminCenter cair.
- [x] **Cancelamento cooperativo (1.7.0)**: `JobCancelled`, `is_cancelled()`,
      `raise_if_cancelled()`, webhook `job.cancel_run`, mapa `_active_runs`
      thread-safe; `run_job` reporta `status='cancelled'` automaticamente.
- [x] **`existing_run_id` em `run_job` (1.7.0)**: webhook `job.run_now` aceita
      `run_id` no payload e reutiliza em vez de criar paralelo —
      evita órfão em 0% no fluxo `trigger-with-attachment`.
- [x] **Jobs manual-only (1.7.0)**: jobs sem `cron_expression` ficam fora do
      APScheduler; executam só via webhook "Rodar agora".
- [x] **`start(with_polling=False)` por default (1.7.0)**: webhook é o canal
      principal; polling vira opt-in para ambientes sem ingresso.

### AdminCenterService (1.7.0)
- [x] Auto-população de `organization_id`/`product_id`/`environment_id` a
      partir do JWT response em `_get_access_token` (migration 0022 do
      AdminCenter). `is_valid()` agora exige só `api_url+api_key`.
- [x] `log_application` com campos top-level (`logger_name`, `module_name`,
      `function_name`, `line_number`, `exception_type`, `exception_message`,
      `stack_trace`) batendo 1-a-1 com a tabela `application_logs`.
- [x] Injeção automática de `run_id`+`job_slug` em `extra_data` quando
      `log_application` é emitido dentro de handler de job
      (`current_run_context()` em `jobs.py`, thread-local).
- [x] `get_application_logs(...)` síncrono com filtros (level/logger/message/
      datas/`extra_data_filter`) e paginação.

### Auth middleware
- [x] `AdminCenterAuth` em modo LOCAL (HS256 + SECRET_KEY) e REMOTE.
- [x] `get_authenticated_user`, `require_product_access`,
      `login_via_admincenter`.

### Database connections (1.5.0)
- [x] DTO `ResolvedConnection` espelhando o backend (id, alias, engine,
      host, port, database_name, schema_name, username/password decriptados,
      use_tunnel, tunnel_type, tunnel_config, access_level,
      allowed_schemas/tables, denied_statements, version, expires_at).
- [x] `ConnectionResolver` com cache em memória por alias, invalidação por
      `version` mismatch e thread-safe.
- [x] `AdminCenterService.resolve_connection(alias=, connection_id=,
      force_refresh=)` chamando `GET /api/database-connection/resolve`.
- [x] `get_db_connection(alias)` → `psycopg2.connection`.
- [x] `get_db_engine(alias, **kwargs)` → SQLAlchemy `Engine`.
- [x] `get_db_session(alias)` → context manager com commit/rollback automático.
- [x] `invalidate_connection_cache(alias?)` para invalidar manualmente.
- [x] Túnel SSH transparente via `sshtunnel.SSHTunnelForwarder` (lazy import).
- [x] Túnel Cloudflare Access via override de host/porta para o cloudflared
      local.
- [x] Lazy imports de `psycopg2`/`sqlalchemy`/`sshtunnel` — extra opcional
      `pip install "automaxia-utils[database]"`.
- [x] `shutdown()` do AdminCenterService fecha túneis SSH abertos.

---

## 🛠 Todo (próximos passos imediatos)

### Versionamento & release
- [x] **LIB-01** — **`CHANGELOG.md` existe** e é o histórico canônico desde a
      1.14.0 (a tabela da [SPEC §12](SPEC.md) ficou como índice das versões).
      Fechado ao conferir em 08/09/2026.
- [x] **LIB-33** — **1.18.0 publicada no mesmo dia** (21/09/2026, commit
      `c3001a6`, tag `v1.18.0`, 144 testes verdes): `ResolvedConnection.metrics`.
      Lição da 1.17.0 aplicada — não ficou nem um dia só no monorepo.
- [x] **LIB-07** — **1.17.0 publicada** em 21/09/2026 (commit `dc30e6c` no `main`
      de `Automaxia/automaxia-shared-utils`, tag `v1.17.0`; 142 testes verdes).
      A 1.16.0 (BigQuery) e a 1.17.0 (`sql_allowlist`) estavam só no monorepo
      desde 15 e 17/09: o CI do `talk-api` e do `vision-api` falhava em todo
      push com "verificador indisponível", porque o que eles instalam é o `main`
      publicado. A 1.16.0 não ganhou tag própria (foi junto).
      ⚠️ O remoto mudou de nome: `automaxia/…` responde com redirect para
      `Automaxia/automaxia-shared-utils`.
- [x] **LIB-06** — **1.15.1 publicada** em 08/09/2026 (commit `e3c847f` no `main`
      de `automaxia/automaxia-shared-utils`, mais a tag `v1.15.1`), com os 116
      testes verdes. Até então o `main` estava em 1.15.0 e a correção do envelope
      (recusa de escrita chegando como `201`) não tinha alcançado pod nenhum.
      Fechou a metade viva do `ST-31`/`OF-03` e destravou o `LIB-03`.
      - Foi junto o sync do monorepo: renomeação Turing → Harvest nos
        comentários/exemplos (`ST-32`) e os docs no padrão MAIÚSCULO
        (`docs/spec.md` → `docs/SPEC.md`, idem TASKS).
- [ ] **LIB-02** — Tags Git para as versões **intermediárias**. Conferido no
      remoto em 08/09/2026: existem `v1.8.0`, `v1.12.0` e agora `v1.15.1` —
      1.9/1.10/1.11 e a linha 1.13–1.15.0 continuam sem tag. Já dá para pinar a
      versão corrente; o que falta é poder pinar as antigas.
- [ ] **LIB-03** — **Pin de versão nos consumidores.** Todos instalam
      `git+…@main` sem pin (conferido em 08/09/2026 nos `requirements.txt` de
      `harvest-api`, `office-api` e irmãos); com `cache-from: type=gha` a lib
      congela na versão do dia em que a camada nasceu. Combinar `no-cache-filters`
      no estágio de deps + `@v1.15.1` no `requirements.txt`. **Destravado em
      08/09/2026** pelo `LIB-06`: a tag existe, então agora é só fazer.
- [ ] **LIB-04** — Documentar processo de release (bump em `setup.py`
      **e em `automaxia_utils/__init__.py`** — os dois divergiram até 08/09/2026 —,
      tag, push, atualizar consumidores).
- [ ] **LIB-05** — Trocar o **PAT em texto puro** no remote do repositório por
      credential helper e revogar a chave.
- [x] Script `reinstall_in_consumers.ps1` (PowerShell) na raiz do repo:
      varre uma raiz (`D:\Automaxia\clientes` por default), encontra todas as
      venvs com `automaxia_utils` instalado e reinstala `--force-reinstall
      --no-deps` apontando para a source local. Suporta `-DryRun` e `-Exclude`.
      Útil em dev local pra propagar mudanças sem `pip install` manual por venv.

### JobRunner
- [ ] **LIB-11** — Suíte de testes (HMAC mismatch, polling, force_run_at, lifecycle
      completo).
- [ ] **LIB-12** — **Retry com backoff** ao reportar `progress`/`finish` em caso de 5xx.
- [ ] **LIB-13** — **Graceful shutdown** com timeout — esperar handlers em execução
      antes de derrubar o servidor.
- [ ] **LIB-14** — Validar que `webhook_url` no Job recebido é resolvível (URL absoluta
      OU relativa com `base_url` do environment) — útil pra dar feedback
      antes do trigger.
- [ ] **LIB-15** — Health check expor versão da lib (`GET /healthz` → `{ok: true,
      service: "automaxia-utils", version: automaxia_utils.__version__}`).
- [ ] **LIB-16** — Endpoint local `GET /jobs` (somente loopback) para debug — lista os
      handlers registrados e a config atual.

### Resiliência
- [ ] **LIB-17** — **Backoff exponencial** no batch worker quando `admincenter-api`
      retorna 5xx em sequência.
- [ ] **LIB-18** — Persistir fila de logs em arquivo local quando AdminCenter ficar fora
      por X minutos (replay no reconnect).
- [ ] **LIB-19** — Métrica interna de `queue_size` exposta para o produto consumidor
      monitorar.

### Database connections
- [ ] **LIB-20** — Suíte de testes (cache TTL, version bump, túnel mock, SQLAlchemy DSN
      por engine).
- [ ] **LIB-21** — Suporte oficial a **MySQL** (`pymysql`), **SQL Server** (`pyodbc`) e
      **Oracle** (`cx_Oracle`) — hoje só Postgres é validado em runtime.
- [ ] **LIB-22** — **Pool de túneis SSH** compartilhado entre processos via socket Unix
      (Linux) — hoje cada processo abre seu forwarder.
- [ ] **LIB-23** — Métrica de `cache_hit_ratio` para `/resolve`.
- [ ] **LIB-24** — Fallback offline: persistir último `ResolvedConnection` em disco
      criptografado com chave do `MASTER_KEY` local — usado se AdminCenter
      ficar fora durante runtime crítico.

### Token tracking
- [ ] **LIB-25** — Suporte a **streaming responses** (OpenAI `stream=True`) — hoje só
      funciona em respostas completas.
- [ ] **LIB-26** — **Pricing por região** (Anthropic Bedrock difere do Anthropic direto).
- [ ] **LIB-27** — Detecção de uso de **prompt cache** (Anthropic prompt caching beta).

### Auth middleware
- [ ] **LIB-28** — Cache de validação **REMOTE** (TTL curto) para reduzir round-trips.
- [ ] **LIB-29** — Suporte a `audience`/`issuer` no JWT — hoje aceita qualquer JWT
      assinado com a `SECRET_KEY` correta.

### Documentação
- [ ] **LIB-30** — Diagrama de sequência: produto → JobRunner → AdminCenter (rodar agora).
- [ ] **LIB-31** — Diagrama de threads/processos: main app + batch worker + jobs http +
      jobs poll + APScheduler.
- [ ] **LIB-32** — Guia de troubleshooting avançado (debug de batch, timeouts, HMAC).

---

## 📋 Backlog (melhorias futuras)

> Wish-list: sem ID. Ganha `LIB-##` quando entra no backlog comprometido acima.

### Distribuição
- [ ] Publicar no **PyPI** (hoje só Git+HTTPS).
- [ ] Wheel + sdist no release (`python -m build` + `twine upload`).
- [ ] Documentar matriz de compatibilidade Python (3.8/3.9/3.10/3.11/3.12/3.13).

### Observabilidade
- [ ] Métricas **Prometheus** opcionais (`automaxia_utils[metrics]`):
      tokens, latência batch, fila, jobs.
- [ ] Tracing OpenTelemetry em chamadas HTTP do `AdminCenterService`.

### JobRunner
- [ ] **Distributed lock** (Redis) — em deploys com múltiplos workers do
      produto, garantir que apenas um execute o job.
- [ ] Suporte a **pré e pós-handlers** globais (ex.: rotação de log file por
      execução).
- [x] ~~**Cancelamento de run em execução**~~ — implementado em **1.7.0** via
      webhook `job.cancel_run` + API cooperativa (`JobCancelled`,
      `is_cancelled`, `raise_if_cancelled`).
- [ ] Suporte a **políticas de retry** definidas no painel.

### Auth
- [ ] Suporte a **OAuth2** (Google/Microsoft) com mapeamento para usuário
      AdminCenter — hoje é só email/senha.
- [ ] **mTLS** opcional para webhook (alternativa ao HMAC, em ambientes
      regulados).

### Token tracking / Custos
- [ ] **Sumário diário** dos custos por produto enviado por email (via
      AdminCenter).
- [ ] Integração com **AWS Bedrock**, **Azure OpenAI**, **Vertex AI**.
- [ ] Custom pricing por contrato negociado (override do LiteLLM).

### LangChain
- [ ] Callback de **prompt caching** automático (descobrir prompt template
      e logar como mesmo `prompt_id` na AdminCenter).
- [ ] Integração com **LangSmith** opcional.

### Testes
- [ ] **Cobertura mínima 70%** com pytest (hoje parcial).
- [ ] Testes de **carga** do batch worker (queue cheia, drops, timeouts).
- [ ] **Mock do AdminCenter** (servidor fake em pytest) para testes
      end-to-end sem precisar do backend real.

### DX
- [ ] Tipos estritos com `mypy --strict`.
- [ ] **Pre-commit hooks** (black, flake8, mypy, isort).
- [ ] Documentação API gerada automaticamente (Sphinx ou MkDocs com
      `mkdocstrings`).

---

## ⚠️ Lacunas conhecidas

- **`AdminCenterContext` não chama `shutdown()`** — só `flush()`. Worker
  segue vivo até o processo sair. Em produtos efêmeros (CLIs), pode atrasar
  exit em até `batch_interval` segundos.
- **Sem versionamento de endpoints** — quando o `admincenter-api` mudar
  contrato, a lib quebra silenciosamente. Mitigar com header
  `X-Lib-Version` + checagem no backend.
- **JobRunner é single-process** — não há mecanismo de "primary" vs
  "replica" embutido. Em deploys com mais de uma réplica, todos disparam
  o mesmo cron simultaneamente. Solução atual: deploy de scheduler como
  Deployment com `replicas: 1`.
- **`get_secret` cache** não tem TTL — invalida só em
  `reset_admin_center_service()`. Útil em runs longos onde rotacionar
  secret no painel não toma efeito até reiniciar.
- **`ConnectionResolver` cache TTL = 5min** — herdado do `expires_at` do
  servidor. Em produtos com runs muito curtos, a primeira chamada paga
  round-trip; ok. Em runs longos com rotação de senha, vale chamar
  `admin.invalidate_connection_cache(alias)` antes ou usar
  `force_refresh=True`. A invalidação automática por `version` cobre o
  caso comum, mas só dispara na próxima chamada — não é proativa.
- **`AdminCenterAuth` LOCAL** assume que produto e AdminCenter têm a mesma
  `SECRET_KEY`. Risco de vazamento se distribuir mal.
- **Sem retry de webhook outbound** entre lib e AdminCenter — se o produto
  está atrás de NAT/proxy intermitente, eventos podem ser perdidos
  (mitigado pelo `force_run_at` no AdminCenter).

## 1.19.0 — produtos derivados e fluxos (2026-09-25)

- [x] **LIB-34** — `product_scope` + `product_id` explícito na telemetria (SDD
      §5.12). Testes em `tests/test_escopo_produto.py`.
- [x] **LIB-35** — `JobRunner(produtos_filhos=True)` + `register_derivados`.
      Testes em `tests/test_jobrunner_filhos.py`.
- [x] **LIB-36** — `automaxia_utils.flows` (`FlowRunner`, ferramentas,
      validação). Testes em `tests/test_flow_runner.py`.
- [x] **LIB-37** — Lote de passos por produto e `execution_scope` em ContextVar.
- [ ] **LIB-38** — **Publicar a 1.19.0** no repositório da lib no mesmo dia do
      commit no monorepo, e conferir `automaxia_utils.__version__` **no pod** do
      Forge (§2.1 do SDD do ecossistema: lib só no monorepo é código que nenhum
      consumidor tem).
- [ ] **LIB-39** — `FlowRunner` assíncrono (hoje síncrono, com ramos em threads).
      O Forge roda no `forge-worker`; na tela, via `run_in_threadpool`.
