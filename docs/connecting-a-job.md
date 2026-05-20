# Conectando um Job ao AdminCenter

Guia ponta-a-ponta para plugar um produto novo na tela de **Jobs Agendados** do AdminCenter — cron, "Rodar agora", pause/resume e progresso ao vivo.

---

## Visão geral

```
                   ┌─────────────────────────────┐
                   │       AdminCenter UI        │
                   │  (Jobs Agendados / botões)  │
                   └────────────┬────────────────┘
                                │ HTTP (JWT do painel)
                                ▼
   ┌────────────────────────────────────────────────────┐
   │              admincenter-api  :8002                │
   │  • CRUD de product_jobs                            │
   │  • POST /job/{id}/trigger ("Rodar agora")          │
   │  • GET  /job/{id}/connection-status (testa agente) │
   └────┬───────────────────────────┬───────────────────┘
        │ webhook HTTP HMAC          │ polling REST + WS
        ▼                            ▼
   ┌────────────────────────────────────────────────────┐
   │  Produto (ex.: folha-pagamento)  :8003 /control    │
   │  automaxia_utils.JobRunner                         │
   │  • registra slugs e handlers                       │
   │  • APScheduler local com cron sincronizado         │
   │  • report_progress() durante a execução            │
   └────────────────────────────────────────────────────┘
```

A **fonte da verdade** é o AdminCenter. O produto não tem cron próprio — ele só **executa** o que o painel mandar.

---

## 1. Requisitos no produto

### 1.1 Instalar a lib

```bash
pip install "automaxia-utils @ git+https://github.com/automaxia/automaxia-shared-utils.git"
# ou, em desenvolvimento, em modo editável apontando pro disco:
pip install -e D:\Automaxia\clientes\automaxia\admincenter\automaxia-shared-utils
```

A versão precisa expor `JobRunner` (≥ 1.4.0). Confira:

```bash
python -c "from automaxia_utils import JobRunner; print('OK', JobRunner)"
```

### 1.2 Variáveis de ambiente (`.env`)

```env
# Identificação do produto/ambiente no AdminCenter
ADMIN_CENTER_ENABLED=true
ADMIN_CENTER_URL=https://admincenter-api.automaxia.com.br/api
ADMIN_CENTER_URL_LOCAL=http://127.0.0.1:8002/api      # usado quando ENVIRONMENT=development
ADMIN_CENTER_API_KEY=sk_test_...                       # sk_test_* ou sk_live_*
ADMIN_CENTER_ORGANIZATION_ID=<uuid>
ADMIN_CENTER_PRODUCT_ID=<uuid>
ADMIN_CENTER_ENVIRONMENT_ID=<uuid-prod>
ADMIN_CENTER_ENVIRONMENT_ID_DEV=<uuid-dev>             # opcional — a lib alterna automático

# Webhook do JobRunner — porta onde o produto escuta comandos do AdminCenter
ADMIN_CENTER_JOBS_WEBHOOK_PORT=8003
ADMIN_CENTER_JOBS_WEBHOOK_SECRET=<random-32-bytes>     # mesmo valor de products.webhook_secret

ENVIRONMENT=development                                 # ou production
```

> ⚠ A porta do webhook **não pode colidir** com a do AdminCenter local (8002). Use 8003+.

> ⚠ Em `ENVIRONMENT=development`, a lib troca automaticamente: `ADMIN_CENTER_URL` → `ADMIN_CENTER_URL_LOCAL` e `ADMIN_CENTER_ENVIRONMENT_ID` → `ADMIN_CENTER_ENVIRONMENT_ID_DEV` (se existir).

### 1.3 Registrar handlers e iniciar o JobRunner

Convenção de slug: `{produto}.{tarefa}` em snake_case (ex.: `folha_pagamento.rodada`, `rpa_boletos.relatorio`). É esse slug que o AdminCenter vai mandar de volta — ele precisa **bater exatamente** com o cadastrado no painel.

```python
# app/scheduler.py
from automaxia_utils import get_admin_center_service, JobRunner
from app.logger import get_logger

# Loggers internos da lib usam logging.getLogger(__name__) e ficam em WARNING
# por default. Inicialize-os pelo seu get_logger para ver o que está acontecendo.
for _name in ("automaxia_utils", "AdminCenterService"):
    get_logger(_name)


def _rodada():
    """Handler para o slug 'folha_pagamento.rodada'."""
    from automaxia_utils import get_admin_center_service
    runner = get_admin_center_service()  # mesmo singleton
    # ... seu fluxo ...
    runner.report_progress(30, "Lendo planilha")
    # ...
    runner.report_progress(100, "Concluído")


def start():
    svc = get_admin_center_service()
    runner = JobRunner(svc)
    runner.register("folha_pagamento.rodada", _rodada)
    runner.start(block=True)
```

`runner.start(block=True)` sobe:
- **HTTP server `BaseHTTP` na porta `ADMIN_CENTER_JOBS_WEBHOOK_PORT`** com 2 rotas:
  - `POST /control` — recebe comandos do AdminCenter (run_now, pause, resume, cancel) com assinatura HMAC.
  - `GET /control/health` — health check (usado pelo botão "Testar conexão").
- **APScheduler local** que sincroniza cron com o AdminCenter via polling REST.

---

## 2. Cadastrando o job no AdminCenter

Pelo painel **Jobs Agendados** → **Novo Job** (ou via SQL direto em `product_jobs`), informe:

| Campo | Obrigatório | Observação |
|---|---|---|
| `product_id` | ✅ | UUID do produto cadastrado |
| `environment_id` | ✅ | UUID do ambiente (dev/prod). Não deixar NULL — senão o produto não recebe |
| `slug` | ✅ | Mesmo slug registrado no `runner.register(...)` |
| `name` | ✅ | Nome amigável exibido na lista |
| `cron_expression` | depende | Padrão crontab (5 campos). Vazio = job só roda via "Rodar agora" |
| `timezone` | ✅ | Ex.: `America/Sao_Paulo` |
| `is_enabled` | ✅ | `true` para o cron disparar |
| `status` | ✅ | `active` (não `paused`) |
| `webhook_url` | ✅* | URL completa do produto + path `/control`, ex.: `http://127.0.0.1:8003/control` |

> *Se `webhook_url` for vazio, o "Rodar agora" não disponibiliza entrega imediata; cai apenas no `force_run_at`, lido pelo polling do produto (latência de até alguns minutos).

> O `webhook_secret` fica em `products.webhook_secret` (compartilhado por todos os jobs do mesmo produto). O backend assina o body com HMAC-SHA256 e o produto valida via `ADMIN_CENTER_JOBS_WEBHOOK_SECRET`.

---

## 3. Como testar a conexão

### 3.1 Pelo painel

Na tela **Jobs Agendados**, ao lado do nome de cada job aparece um indicador:

- ☁️ **verde** (`cloud_done`) — agente respondeu em `/control/health`
- ☁️ **vermelho** (`cloud_off`) — agente não respondeu (offline ou URL errada)
- 🔄 cinza girando — verificando

Clicar no ícone abre um toastr com o detalhe (latência, status code, motivo).

### 3.2 Por API

```bash
TOKEN=<jwt-do-painel>
JOB_ID=<uuid-do-job>
curl -H "Authorization: Bearer $TOKEN" \
     http://127.0.0.1:8002/api/job/$JOB_ID/connection-status
```

Resposta esperada quando tudo OK:

```json
{
  "ws":      { "connected": 0 },
  "webhook": { "url": "http://127.0.0.1:8003/control",
               "reachable": true, "status_code": 200, "ms": 12,
               "detail": "http://127.0.0.1:8003/control/health" }
}
```

> `ws.connected: 0` é **normal** com `automaxia_utils ≥ 1.6.0` — a lib só usa webhook HTTP. WS aparece >0 só em produtos que ainda usam o shim aiohttp legado.

### 3.3 Disparando "Rodar agora"

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
     http://127.0.0.1:8002/api/job/$JOB_ID/trigger
```

Resposta com `delivery.webhook_ok: true` significa que o produto recebeu e aceitou o comando.

---

## 4. Troubleshooting

### O job aparece "offline" no painel

Verifique nesta ordem:

1. **O scheduler do produto está rodando?**
   ```bash
   netstat -ano | findstr ":8003"
   ```
   Se vazio: `python main.py scheduler`.

2. **`webhook_url` no banco aponta pra `/control`?**
   - ❌ `http://127.0.0.1:8003/folha-pagamento` → 404
   - ✅ `http://127.0.0.1:8003/control`

3. **Porta do produto é diferente da do AdminCenter (8002)?**
   - Nunca usar 8002 em `ADMIN_CENTER_JOBS_WEBHOOK_PORT`.

4. **A lib instalada tem `JobRunner`?**
   ```bash
   python -c "from automaxia_utils import JobRunner"
   ```
   Se falhar com `ImportError`, atualize a lib (ver §1.1).

### "Rodar agora" não dispara nada no produto

A resposta do `/trigger` mostra exatamente o motivo. Se `webhook_ok: false`:

- `404 Not Found` → `webhook_url` aponta para path errado (deve ser `/control`).
- `Connection refused` → produto não está rodando ou em outra porta.
- `401 Unauthorized` → secret no produto difere de `products.webhook_secret`.

### Os logs do scheduler param em "scheduler iniciado"

Os loggers internos da lib (`automaxia_utils.*`, `AdminCenterService`) usam `logging.getLogger(__name__)`. Se o seu `app/logger.py` configura cada logger com `propagate=False`, esses loggers ficam herdando o nível default do root (`WARNING`) e os `INFO` somem.

Adicione no início do scheduler:

```python
from app.logger import get_logger
for _name in ("automaxia_utils", "AdminCenterService"):
    get_logger(_name)
```

### Job só dispara em manual, ignora o cron

- `is_enabled` precisa ser `true`.
- `status` precisa ser `active` (não `paused`).
- `cron_expression` precisa ser válida (5 campos crontab).
- `environment_id` **não pode ser NULL** — caso contrário, o produto consultando `?environment_id=...` no `/agent/job` não vê o job.

### `last_status` ficou travado em `running` ou `cancelling`

A última execução não terminou (processo morto antes do `_finish_run`). Não é bloqueante: a próxima execução sobrescreve. Para limpar manualmente:

```sql
UPDATE product_jobs SET last_status='cancelled' WHERE id='<job-id>';
```

---

## Checklist final

Antes de subir um produto novo:

- [ ] `automaxia_utils` ≥ 1.4.0 instalado
- [ ] `.env` com todas as 7 variáveis `ADMIN_CENTER_*` preenchidas
- [ ] Porta `ADMIN_CENTER_JOBS_WEBHOOK_PORT` livre e diferente de 8002
- [ ] `runner.register("<slug>", handler)` antes do `runner.start()`
- [ ] No AdminCenter: job criado com mesmo slug, `webhook_url=...:<porta>/control`, `is_enabled=true`, `environment_id` setado
- [ ] Botão "Testar conexão" verde na tela de Jobs
- [ ] "Rodar agora" devolve `delivery.webhook_ok: true`
