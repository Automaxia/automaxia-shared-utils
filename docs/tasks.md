# tasks.md — automaxia-shared-utils (infrabalance)

Backlog técnico da lib. Atualize ao concluir/criar itens.

---

## ✅ Done

### Núcleo
- [x] `AdminCenterConfig` com `from_env()` e settings via `.env`.
- [x] Singleton thread-safe (`get_admin_center_service`,
      `reset_admin_center_service`).
- [x] Exchange API key → JWT em `/auth/gerar-token/api-key`.
- [x] Refresh proativo do JWT (~55min) e em 401.

### Logging
- [x] `log_application(level, message, context)`.
- [x] `log_execution(endpoint, method, status_code, response_time_ms)`.
- [x] `log_process(process_name, status, duration_ms, metadata)`.
- [x] Batch worker assíncrono (queue + thread).
- [x] `flush()` síncrono para drain manual.
- [x] `AdminCenterContext` (context manager).
- [x] `@track_execution` (decorator com lifecycle started/completed/failed).

### Cofre & catálogo
- [x] `get_secret(name)`.
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

### Auth middleware
- [x] `AdminCenterAuth` em modo LOCAL (HS256 + SECRET_KEY) e REMOTE.
- [x] `get_authenticated_user`, `require_product_access`,
      `login_via_admincenter`.

### Database connections (1.4.0)
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

### Integração com produtos consumidores (1.4.0)
- [x] Padrão `DB_CONNECTION_ALIAS` no `.env` dos produtos substituindo
      variáveis brutas `DB_HOST/DB_USER/DB_PASSWORD`.
- [x] Helper `connection_resolver.py` em `dashboard-backend/core/`
      (`resolve_db_config_via_broker`, `to_db_config_dict`).
- [x] Helper `connection_resolver.py` em `datachatai-backend/src/core/`
      (`resolve_connection_via_broker`, `merge_resolved_into_config_dict`
      com suporte a prefixo `db_` e `metrica_db_`).
- [x] `Config.__init__` do datachatai resolve `db_connection_alias` e
      `metrica_db_connection_alias` antes de montar `db_connection` string.
- [x] `settings.py` do dashboard sobrescreve `DB_CONFIG` via broker quando
      alias setado; `reload_admin_center_configs()` invalida cache e
      re-resolve.
- [x] Backward-compat 100%: se broker offline ou alias vazio, produto
      cai pro fluxo legado (`DB_HOST/DB_USER/DB_PASSWORD` via `.env` ou
      `get_variable()` do AdminCenter).
- [x] `requirements.txt` dos dois produtos atualizado para
      `automaxia-utils[database]` (puxa psycopg2-binary, sqlalchemy,
      sshtunnel).

---

## 🛠 Todo (próximos passos imediatos)

### Versionamento & release
- [ ] Adicionar **CHANGELOG.md** com histórico desde v1.0.0.
- [ ] **Tags Git formais** (`v1.4.0`, …). Hoje só commit hash no
      `requirements.txt` dos produtos.
- [ ] Documentar processo de release (bump em `setup.py`, tag, push,
      atualizar consumidores).

### Resiliência
- [ ] **Backoff exponencial** no batch worker quando `plataforma-backend`
      retorna 5xx em sequência.
- [ ] Persistir fila de logs em arquivo local quando a plataforma ficar
      fora por X minutos (replay no reconnect).
- [ ] Métrica interna de `queue_size` exposta para o produto consumidor
      monitorar.

### Database connections
- [ ] Suíte de testes (cache TTL, version bump, túnel mock, SQLAlchemy DSN
      por engine).
- [ ] Suporte oficial a **MySQL** (`pymysql`), **SQL Server** (`pyodbc`) e
      **Oracle** (`cx_Oracle`) — hoje só Postgres é validado em runtime.
- [ ] **Pool de túneis SSH** compartilhado entre processos via socket Unix
      (Linux) — hoje cada processo abre seu forwarder.
- [ ] Métrica de `cache_hit_ratio` para `/resolve`.
- [ ] Fallback offline: persistir último `ResolvedConnection` em disco
      criptografado com chave do `MASTER_KEY` local — usado se a
      plataforma ficar fora durante runtime crítico.

### Token tracking
- [ ] Suporte a **streaming responses** (OpenAI `stream=True`).
- [ ] **Pricing por região** (Anthropic Bedrock difere do Anthropic direto).
- [ ] Detecção de uso de **prompt cache** (Anthropic prompt caching beta).

### Auth middleware
- [ ] Cache de validação **REMOTE** (TTL curto) para reduzir round-trips.
- [ ] Suporte a `audience`/`issuer` no JWT.

### Documentação
- [ ] Diagrama de threads/processos: main app + batch worker +
      ConnectionResolver.
- [ ] Guia de troubleshooting avançado (debug de batch, timeouts, túnel SSH).

---

## 📋 Backlog (melhorias futuras)

### Distribuição
- [ ] Publicar no **PyPI** (hoje só Git+HTTPS).
- [ ] Wheel + sdist no release (`python -m build` + `twine upload`).
- [ ] Documentar matriz de compatibilidade Python (3.8/3.9/3.10/3.11/3.12/3.13).

### Observabilidade
- [ ] Métricas **Prometheus** opcionais (`automaxia_utils[metrics]`):
      tokens, latência batch, fila, conexões resolvidas.
- [ ] Tracing OpenTelemetry em chamadas HTTP do `AdminCenterService`.

### Auth
- [ ] Suporte a **OAuth2** (Google/Microsoft) com mapeamento para usuário
      da plataforma.
- [ ] **mTLS** opcional para webhook (alternativa ao HMAC, em ambientes
      regulados).

### Token tracking / Custos
- [ ] **Sumário diário** dos custos por produto enviado por email.
- [ ] Integração com **AWS Bedrock**, **Azure OpenAI**, **Vertex AI**.
- [ ] Custom pricing por contrato negociado (override do LiteLLM).

### LangChain
- [ ] Callback de **prompt caching** automático.
- [ ] Integração com **LangSmith** opcional.

### Testes
- [ ] **Cobertura mínima 70%** com pytest.
- [ ] Testes de **carga** do batch worker (queue cheia, drops, timeouts).
- [ ] **Mock da plataforma** (servidor fake em pytest) para testes
      end-to-end sem precisar do backend real.

### DX
- [ ] Tipos estritos com `mypy --strict`.
- [ ] **Pre-commit hooks** (black, flake8, mypy, isort).
- [ ] Documentação API gerada automaticamente (Sphinx ou MkDocs com
      `mkdocstrings`).

---

## ⚠️ Lacunas conhecidas

- **Sem versionamento de endpoints** — quando o `plataforma-backend` mudar
  contrato, a lib quebra silenciosamente. Mitigar com header
  `X-Lib-Version` + checagem no backend.
- **`get_secret` cache** não tem TTL — invalida só em
  `reset_admin_center_service()`.
- **`ConnectionResolver` cache TTL = 5min** — herdado do `expires_at` do
  servidor. Em runs longos com rotação de senha, vale chamar
  `admin.invalidate_connection_cache(alias)` antes ou usar
  `force_refresh=True`. A invalidação automática por `version` cobre o
  caso comum, mas só dispara na próxima chamada — não é proativa.
- **`AdminCenterAuth` LOCAL** assume que produto e plataforma têm a mesma
  `SECRET_KEY`. Risco de vazamento se distribuir mal.
- **Auth requer FastAPI** — projetos não-FastAPI que importarem o pacote
  inteiro vão falhar. Considerar tornar import de auth opcional via
  try/except `ImportError` no `__init__.py` (já implementado em outros
  forks da lib).
