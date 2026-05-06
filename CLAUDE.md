# claud.md — automaxia-shared-utils (infrabalance)

Manual da IA para a lib Python compartilhada que liga produtos da
Infrabalance/Linedata à plataforma. Leia antes de mexer.

---

## 1. O que é

`automaxia-utils` (pacote `automaxia_utils`) — biblioteca Python instalada
nos produtos finais (RPAs, APIs, integrações) para falar com a plataforma
e fornecer infraestrutura compartilhada:

- **Plataforma integration**: logs, secrets, environment variables,
  prompts centralizados, tracking de tokens/custos.
- **Database connections broker** (1.4.0+): resolve credenciais de banco
  cadastradas na plataforma (`/database-connection/resolve`), abre
  conexões psycopg2 ou SQLAlchemy `Engine`/`Session`, gerencia túnel
  SSH/Cloudflare e cacheia por TTL com invalidação por `version`.
- **Token tracking multi-provider**: contagem hierárquica (response.usage →
  LiteLLM → tiktoken → fallback) e cálculo de custos com cotação USD/BRL.
- **Auth middleware**: `AdminCenterAuth` para FastAPI (JWT local ou remote)
  + `get_current_user` + `require_product_access`.

Versão atual: **1.4.0**.

---

## 2. Stack

| Camada | Tecnologia |
|---|---|
| Linguagem | Python 3.8+ (compatível até 3.13) |
| HTTP | requests, httpx |
| Token counters | tiktoken, litellm |
| Configs | python-decouple, pydantic-settings |
| Auth | python-jose (HS256) — `[auth]` extra (FastAPI requerido) |
| Database broker | psycopg2-binary, sqlalchemy, sshtunnel — `[database]` extra |
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
├── README.md
├── setup.py                        # versão 1.4.0
├── requirements.txt
├── automaxia_utils/
│   ├── __init__.py                 # API pública re-exportada
│   ├── admin_center/
│   │   ├── __init__.py
│   │   ├── service.py              # AdminCenterService
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
└── tests/
```

---

## 4. Como rodar

```powershell
# Instalar a partir do repo local (editable)
pip install -e .

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
ADMIN_CENTER_URL=https://plataforma-api.linedata.com.br/api
ADMIN_CENTER_API_KEY=sk_test_…
ADMIN_CENTER_ORGANIZATION_ID=…
ADMIN_CENTER_PRODUCT_ID=…
ADMIN_CENTER_ENVIRONMENT_ID=…

# Conexões de banco — o produto NÃO guarda mais host/user/password aqui.
# Apenas o alias da conexão cadastrada no painel da plataforma.
DB_CONNECTION_ALIAS=cliente_prod
METRICA_DB_CONNECTION_ALIAS=metricas_centralizado    # apenas datachatai
```

---

## 5. Padrões de arquitetura

### 5.1 Singleton + lazy

`get_admin_center_service()` cria uma única instância. Imports caros
(litellm, psycopg2, sshtunnel) só acontecem quando o método que os usa é
chamado.

### 5.2 Batch worker assíncrono

Logs (`log_application`, `log_execution`, `log_process`) e usage de tokens
(`track_token_usage`) entram numa fila e um worker thread despacha em lote.
Configurável via `AdminCenterConfig.batch_size` e `batch_interval`.

### 5.3 Auth: API key → JWT

A lib só envia API key crua para `POST /auth/gerar-token/api-key`. Demais
chamadas usam o JWT obtido (validade 1h, renovado automaticamente em 401
ou ~5min antes da expiração).

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
2. Plataforma API               → preços cadastrados no painel (cache)
3. Fallback hardcoded           → mar/2026
```

### 5.6 ConnectionResolver (broker de banco)

`AdminCenterService.resolve_connection(alias=...)` chama
`GET /api/database-connection/resolve` na plataforma e devolve um
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

### 5.7 Integração com produtos consumidores (padrão alias)

Como `datachatai-backend`, `dashboard-backend` e demais produtos consomem
o broker hoje:

#### Antes (≤ 1.3.0)

O produto recebia **dados crus** de conexão como variáveis de ambiente
(via `.env` direto ou `admin.get_variable("DB_HOST")` do AdminCenter):

```
DB_HOST=84.247.138.18
DB_PORT=5432
DB_NAME=sabesp
DB_USER=usr_app
DB_PASSWORD=••••••••
DB_SCHEMA=public
```

A senha trafegava como variável de ambiente. Cada produto precisava ter
sua cópia. Rotação exigia atualizar `.env` (ou as variables no painel) e
**reiniciar o produto**.

#### Hoje (1.4.0+)

O produto recebe apenas o **alias** da conexão cadastrada no painel da
plataforma (tabela `database_connections`):

```
DB_CONNECTION_ALIAS=cliente_prod
```

A senha mora **cifrada** (AES-256-GCM) na plataforma. A lib resolve o
alias chamando `GET /api/database-connection/resolve?alias=cliente_prod`,
recebe `ResolvedConnection` com credenciais decriptadas, cacheia por 5
min e usa em runtime. Rotação no painel **bumpa `version`** e a próxima
chamada de `resolve()` pega a credencial nova — produto não reinicia.

#### Dois caminhos de consumo no produto

**(a) Padrão "adapter legado"** — produto continua com `Config.db_host`,
`Config.db_password`, etc. O `Config.__init__` chama um helper
(`core/connection_resolver.py` em cada produto) que popula esses
atributos a partir do `ResolvedConnection`. `DatabaseClient`/
`DatabaseManager` segue exatamente como antes:

```python
# datachatai-backend/src/core/config.py
self.db_connection_alias = config_dict.get('db_connection_alias', '')
if self.db_connection_alias:
    resolved = resolve_connection_via_broker(self.db_connection_alias)
    if resolved:
        merge_resolved_into_config_dict(config_dict, resolved, prefix='db_')

self.db_host = config_dict.get('db_host')
self.db_password = config_dict.get('db_password')
self.db_connection = f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

# Resto do código não muda:
psycopg2.connect(self.config.db_connection)
```

Vantagem: migração não invasiva, sem reescrever o `DatabaseManager`.
Limitação: como abre `psycopg2.connect(string)` direto, **túnel SSH não
é ativado automaticamente**.

**(b) Padrão "direto"** — produto chama os métodos novos da lib e ganha
túnel SSH/Cloudflare transparente:

```python
from automaxia_utils import get_admin_center_service
admin = get_admin_center_service()

# psycopg2 com túnel SSH automático se cadastrado
conn = admin.get_db_connection('cliente_prod')

# SQLAlchemy session com commit/rollback automático
with admin.get_db_session('cliente_prod') as session:
    session.execute(text("SELECT 1"))
```

Migração progressiva recomendada: novos pontos de conexão usam (b);
pontos legados continuam em (a) até serem refatorados.

#### Helper `connection_resolver.py` em cada produto

Para o adapter (a), cada produto consumidor tem um helper local:

- `dashboard-backend/core/connection_resolver.py`:
  - `resolve_db_config_via_broker(alias)` → `ResolvedConnection` ou None.
  - `to_db_config_dict(resolved)` → dict `{host, port, database, user, password, schema}`.
- `datachatai-backend/src/core/connection_resolver.py`:
  - `resolve_connection_via_broker(alias)` → idem.
  - `merge_resolved_into_config_dict(config_dict, resolved, prefix='db_')`
    — sobrescreve as chaves `db_host`/`db_user`/etc. Aceita prefixo
    `metrica_db_` para o banco de métricas centralizado do datachatai.

Ambos falham silenciosamente: se broker indisponível, retornam None e
o produto cai pro fluxo legado (`DB_HOST`/`DB_USER` do `.env`),
**preservando backward-compat 100%**.

#### Autorização: o produto precisa estar autorizado

No painel da plataforma, dentro de cada conexão, aba **Acessos** →
**Conceder acesso**:

| Campo | Valor |
|---|---|
| Principal Type | `product` |
| Principal ID | UUID do produto consumidor |
| Access Level | `read` / `write` / `admin` |

Sem esse registro em `database_access`, o `/resolve` retorna 403 e
loga em `database_access_logs`.

---

## 6. API pública (`__init__.py`)

```python
from automaxia_utils import (
    # Plataforma
    AdminCenterService, AdminCenterConfig,
    get_admin_center_service, reset_admin_center_service,
    AdminCenterContext, track_execution,
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
novos. Quem consumir de submódulos é considerado privado e pode quebrar
entre versões.

---

## 7. Estilo de código

- **Type hints obrigatórios** em assinaturas públicas. Use
  `from __future__ import annotations`.
- **Docstrings curtas** focando no porquê e nos efeitos colaterais (worker,
  IO de rede, threading).
- **Sem `print`**: use `logging.getLogger(__name__)`.
- **Lazy imports** em deps pesadas (litellm, psycopg2, sshtunnel).
- **Threadsafe**: serviços longos guardam `threading.Lock` quando mexem em
  cache (`_token_lock`, `_price_cache_lock`, `_lock` no resolver).
- **Resiliência**: toda chamada externa precisa de try/except + log; nunca
  derruba o produto consumidor.

---

## 8. Workflow esperado

1. **Antes de mudar API pública**: bumpa versão (semver) em `setup.py` e
   `__init__.py`.
2. **Mudou contrato com a plataforma**: alinhe com `plataforma-backend`
   (`spec.md` lá) — endpoints `/auth/gerar-token/api-key`, `/secret`,
   `/prompt`, `/database-connection/resolve`.
3. **Adicionou novo provider**: estenda `track_api_response` em
   `token_tracking/counter.py` e cubra os campos extras na
   `extract_tokens_from_response`.
4. **Publicação**: hoje é via Git tag. Commit em `main`, tag
   `v<major>.<minor>.<patch>`, atualize a referência sha no
   `requirements.txt` dos produtos.

---

## 9. Pontos de atenção (gotchas)

- **JWT expira em 1h**: o método `_ensure_token()` deve ser chamado antes
  de qualquer request autenticada — não cacheie o header `Authorization`
  além do necessário.
- **Cotação USD/BRL**: cache em memória de 30min. Em testes, force valor
  via env var `USD_BRL_RATE` para evitar chamadas externas.
- **Singleton + multi-process**: cada processo tem seu próprio singleton.
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

# Smoke test
python -c "from automaxia_utils import get_admin_center_service; \
            admin = get_admin_center_service(); \
            print(admin.config.api_url)"

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
- Plataforma (servidor): `../plataforma-backend`
