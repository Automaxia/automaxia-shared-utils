# SDD — automaxia-shared-utils

> Desenho interno da lib `automaxia_utils` **1.19.0**.
> Requisitos: [SPEC.md](SPEC.md) · Backlog: [TASKS.md](TASKS.md) ·
> Histórico: [`../CHANGELOG.md`](../CHANGELOG.md)
> Contratos transversais: [`../../../docs/SDD.md`](../../../docs/SDD.md) — **não
> repetidos aqui** (JWT, cofre, auto-registro, silo `mode`, linha do tempo §5.6,
> fluxos §5.11, produtos derivados §5.12).

Última revisão: **2026-09-28** · lib **1.19.0 publicada** (`2d3a4bf`, tag
`v1.19.0`, 25/09/2026); o código da lib não mudou desde então.

> O desenho descrito aqui é o mesmo desde a 1.12.0; o que entrou depois foram
> capacidades novas sobre ele:
>
> - **1.15.0/1.15.1** — linha do tempo de execução (`agent_step`/`log_step`/
>   `execution_scope`, contrato em §5.6 do ecossistema) e o batch worker
>   inspecionando o **envelope** da resposta (recusa de escrita viaja dentro de
>   um HTTP `201`).
> - **1.16.0/1.17.0** — engine `bigquery` no cofre e `sql_allowlist` (verificação
>   de SQL contra `allowed_tables`, fail-closed).
> - **1.18.0** — `ResolvedConnection.metrics` (camada semântica, §5.8 do
>   ecossistema). Só um campo a mais no DTO; nenhum desenho novo.
> - **1.19.0** — **produto em escopo** (`product_scope`, §4.5) e o **motor de
>   fluxos** `automaxia_utils.flows` (§4.6). Foi a primeira mudança de desenho
>   desde a 1.12.0: estado de contexto passou a viver em `ContextVar`, não só em
>   thread-local, para atravessar os ramos paralelos do runner.
>
> Detalhe de cada versão em [`../CHANGELOG.md`](../CHANGELOG.md).

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
| `admin_center/connections.py` | Broker do cofre: `ResolvedConnection` (+ `allowed_tables`, `metrics`) + cache + túnel; BigQuery (`build_bigquery_client`) | lazy: `psycopg2`, `sqlalchemy`, `sshtunnel`, `google-cloud-bigquery` |
| `admin_center/jobs.py` | `JobRunner`: WS + listener HMAC + APScheduler + run lifecycle; modo `produtos_filhos` (1.19.0) | lazy: `apscheduler`, `croniter`, `aiohttp` |
| `auth/middleware.py` | Validação de JWT + RBAC + gate de produto | **FastAPI** (opcional) |
| `registration/client.py` | Manifest (+ `tools`/`flow_entrypoints`, 1.19.0), `POST /product/register`, heartbeat daemon | `requests` |
| `migrations/runner.py` | `alembic upgrade` com retry | **alembic** (opcional) |
| `token_tracking/counter.py` | Contagem e preço multi-provider; agente da requisição (`definir_agente`/`agente_em_uso`) | lazy: `tiktoken`, `litellm` |
| `sql_allowlist.py` | `verificar_sql`/`tabelas_referenciadas`/`filtrar_tabelas` contra a allowlist da conexão (1.17.0) | `sqlglot` (core) |
| `flows/runner.py` | `FlowRunner`, `RegistroDeFerramentas`, `validar_fluxo` (1.19.0) | nenhuma — recebe o `AdminCenterService` e o LLM por injeção |

Nenhum módulo importa outro fora de `admin_center` ↔ `jobs` (run context) — é o
que mantém o pacote utilizável em pedaços. O `flows` segue a regra: não importa
`admin_center`; usa só a interface do objeto `admin` que recebe
(`get_effective_prompt`, `track_token_usage`, `agent_step`, `execution_scope`,
`_produto_efetivo`). Com `admin=None` ele roda sem telemetria nem IA (útil em
teste de ferramenta).

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
| Produto em escopo (1.19.0) | `_product_scope` (`ContextVar`) | por contexto | bloco `product_scope` |
| Execução em curso | `_exec_scope` (`ContextVar`, 1.19.0) + `_step_local` (thread-local) | lock na sequência | bloco `execution_scope` |
| Agente da requisição | `_agente_da_requisicao` (`ContextVar`) | por contexto | `definir_agente` / `agente_em_uso` |

**Threads vivas num produto típico:** main (FastAPI) · batch worker · heartbeat ·
listener HTTP do JobRunner · loop WS · APScheduler · uma thread por run · no
`FlowRunner`, até `paralelo` (padrão 4) threads por execução com ramos paralelos.

**Thread-local × `ContextVar`.** O run context do job e a correlação antiga
(`_step_local`) são thread-local; tudo o que o `FlowRunner` precisa levar para
um ramo paralelo é `ContextVar`, porque o runner despacha o ramo com
`contextvars.copy_context().run` — um thread-local ficaria para trás e o passo
do ramo cairia no produto pai, sem `correlation_id`. A sequência de passos do
`execution_scope` é **compartilhada** entre os ramos (o contexto copiado aponta
para o mesmo `_EscopoExecucao`), por isso o lock. ⚠️ Quem despacha thread por
conta própria (ex.: `ThreadPoolExecutor` no produto) precisa copiar o contexto
do mesmo jeito, ou perde escopo de produto e de execução.

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

**Modo `produtos_filhos` (1.19.0).** `JobRunner(svc, produtos_filhos=True)` pede
`GET /agent/job?incluir_filhos=true` e passa a receber também os jobs dos
produtos derivados que o satélite hospeda. O que muda dentro do runner:

- `_JobConfig.product_id` (preenchido pelo backend desde a `0061`) separa job
  próprio de job de filho (`_eh_filho`: produto do job ≠ `config.product_id`).
- **Chave interna** `<product_id>:<slug>` para job de filho — slug é único só
  dentro de um produto, e dois filhos podem repetir. Scheduler, `run_job`,
  webhook e o polling de `force_run_at` usam essa chave.
- **Um handler só** para todos os filhos (`register_derivados(handler)`): o pai
  não conhece de antemão os slugs, que nascem quando alguém publica uma agenda.
  O handler recebe o `_JobConfig` e roda dentro de
  `svc.product_scope(cfg.product_id)` (§4.5).
- O "rodar agora" do painel chega a job de filho por `force_run_at` — suba com
  `start(with_polling=True)`.

### 4.5 Produto em escopo (1.19.0)

Contrato no SDD do ecossistema §5.12. Do lado da lib:

```
product_id da escrita = explícito  >  product_scope (ContextVar)  >  config.product_id
                         └─ _produto_efetivo()
ambiente da escrita   = o da config SÓ quando o produto é o da config;
                        produto de escopo/explícito vai SEM environment_id
                         └─ _identidade_de_log()
```

- **Quem respeita o escopo:** `log_process`, `log_step`, `agent_step`,
  `track_token_usage` (também aceitam `product_id=` explícito) e a resolução
  `get_effective_prompt`, `list_allowed_agents`, `resolve_agent_id`,
  `invalidate_effective_model_cache`. **Quem não respeita:** `log_execution` e
  `log_application` — são o satélite atendendo a request, então ficam no
  produto da config.
- `product_scope` valida UUID na entrada (`ValueError`): atribuir consumo a um
  produto torto é pior que falhar alto. Reentrante (`ContextVar.reset`).
- `agent_step` **fixa o produto na entrada** da etapa (`_StepHandle._product_id`):
  a linha terminal pode sair fora do escopo e ainda vai para o mesmo produto.
- `_validate_token_usage_payload` só exige `environment_id` quando o produto é o
  da config.
- **Lote de passos: um POST por produto.** `_process_batch` agrupa os passos da
  fila por `product_id` antes do `POST /logs/step` — o backend resolvia o lote
  inteiro pelo primeiro item.
- O backend só aceita a escrita em produto alheio com a chave do **pai** dele
  (admincenter-api `0061`); com AdminCenter anterior a escrita é recusada no
  envelope e aparece como WARNING do batch worker (1.15.1).

`execution_scope` **não** recebe `product_id`: ele só agrupa passos
(correlação + numeração); o produto de cada passo vem do escopo de produto.

### 4.6 Fluxos — `FlowRunner` (1.19.0)

Formato do fluxo (`formato: 1`), tipos de nó, referências e regras: SDD do
ecossistema §5.11.2. Aqui, só o que é da implementação.

```
FlowRunner(admin, registro, chamar_llm,
           max_nos=60, max_tokens=None, max_segundos=None,
           max_chars_entrada=12000, paralelo=4)
  .run(definicao, entrada, modo='real'|'teste', extra=None,
       rotulo_versao=None, ao_passo=None) -> ResultadoFluxo
```

**Quem fornece o quê**

| Peça | Dono | Na lib |
|---|---|---|
| Definição do fluxo (JSON) | o produto (hoje, o Forge em `forge.*`) | recebida em `run(definicao, …)` — a lib **não** busca fluxo em lugar nenhum |
| LLM | o produto | `chamar_llm(PedidoLLM) -> RespostaLLM`; a lib não escolhe provedor nem monta mensagens de API |
| Prompt e modelo | AdminCenter | `admin.get_effective_prompt(slug)` do produto em escopo; sem vínculo ou sem `model_name` → `IaNaoConfigurada` (nunca texto/modelo de reserva) |
| Ferramentas | o produto | `RegistroDeFerramentas`; `@registro.ferramenta(nome, versao, descricao, entradas, saida, selada, efeito)`; função `(ContextoFluxo, entradas, config) -> dict` |
| Declaração no manifest | o produto | `registro.specs()` → `ProductManifest.tools` (`name`, `version`, `descricao`, `entradas`, `saida`, `selada`, `efeito?`) |

**Execução.** Fila de prontos sobre o grafo: um nó roda quando todas as arestas
de entrada estão `fired` ou `dead` e ao menos uma `fired`. Ramo cortado por
`escolha`/`condicao` propaga `dead` (eliminação de caminho morto) para a junção
não esperar para sempre. Aresta com `max_voltas` recoloca o alvo como `forcado`
e conta voltas (`LimiteExcedido` acima do limite). Lote com mais de um nó pronto
vai para um `ThreadPoolExecutor(paralelo)` via `contextvars.copy_context().run`
(§3); resultados aplicados na ordem de numeração.

**Nó agente** (`_rodar_agente`): `sistema` = `generic_content` + `custom_content`
+ `"Instrucao desta etapa:\n" + instrucao` + regra de formato (json/escolha);
`usuario` = cada entrada como bloco `## <nome>`, **cortada em
`max_chars_entrada`** com a marca `[... cortado: N caracteres]`.
`temperatura`/`max_tokens` vêm de `generic_temperature`/`generic_max_tokens` do
effective-prompt. Saída `json`/`escolha` fora do formato → uma nova tentativa
com a correção anexada; na segunda, `FalhaDoNo`. Cada chamada (inclusive a
tentativa recusada) vira um `track_token_usage(agent_slug=…,
endpoint_called="fluxo:<no>")`.

**Nó ferramenta** (`_rodar_ferramenta`): ferramenta ausente no registro ou com
`versao` diferente da gravada no nó → `FalhaDoNo` ("fluxo quebrado"), nunca
pula. **Ferramenta com `efeito` em `modo='teste'` não é chamada**: o nó devolve
`{simulado, enviado: False, faria: {...}}`. Quem decide é o motor.

**Telemetria.** A execução inteira roda dentro de `admin.execution_scope()`
(`ResultadoFluxo.correlation_id`); cada nó `agente`/`ferramenta` é um
`agent_step` (`kind='llm'`/`'step'`, `detail = {node_id, flow_version, volta}`),
com os tokens do nó creditados no `done`. Nó `condicao`/`saida` não grava
passo. O run faturado (`log_process`) é de quem chama o runner — **passo não é
run** (§5.6 do ecossistema).

**Falha.** `run` nunca levanta por falha de nó: `FalhaDoNo`, `IaNaoConfigurada`,
`LimiteExcedido` e qualquer exceção de ferramenta/LLM viram
`ResultadoFluxo.falha` + `no_falha` + uma linha `tipo='erro'` no `traco`.
Levanta só por `modo` inválido ou definição sem `formato: 1` (`FluxoInvalido`).

**`validar_fluxo(definicao, ferramentas, entradas, saidas)`** devolve
`[{nivel, no, msg}]` sem executar: nó solto, referência a nó que não roda antes
ou a campo que ele não devolve, ramo sem destino, laço sem `max_voltas` (1–5),
ferramenta não declarada, **entrada de ferramenta sem origem** (todas as
`entradas` declaradas são obrigatórias — `LIB-40`), ferramenta de efeito sem
`config.destino`, saída sem o primeiro campo de `saidas`. Não confere agente
contra o AdminCenter; o runner confere ao rodar.

**O que a implementação ainda não tem, frente ao §5.11 do ecossistema** (que
descreve a trilha completa, proposta):

- busca do fluxo publicado (`GET /agent-flow/resolve`, cache por `version`) e a
  rota `/flows/testar` — o produto passa a definição e monta o teste;
- tipos `ToolSpec`/`FlowEntrypoint`: o manifest aceita listas de dicts;
- `amostra_para_llm` e **aviso de corte** — o runner corta a entrada em
  silêncio (só a marca no texto); o Forge reconstrói o aviso a partir do
  `traco` (`LIB-42`);
- a chave do `detail` é `flow_version` (o `rotulo_versao` que o produto passa),
  não `flow_version_id`;
- o `ResultadoFluxo.traco` guarda entradas e saídas de cada nó em memória —
  quem chama decide se grava (RN-FL-03 é do produto).

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
| D-11 | Produto em escopo por **`ContextVar`**, não parâmetro em toda chamada (1.19.0) | um satélite construtor (Forge) grava cada execução no produto derivado sem reescrever cada ponto de telemetria; atravessa as threads copiadas pelo runner |
| D-12 | Produto de escopo grava **sem** `environment_id` (1.19.0) | o ambiente da config é do pai; mandá-lo atribuiria o consumo do filho ao ambiente errado |
| D-13 | `FlowRunner` recebe a definição e o LLM por **injeção** (1.19.0) | a lib não escolhe provedor nem sabe onde o fluxo mora (hoje `forge.*`; no futuro `admincenter.agent_flows`) — o interpretador é o mesmo em todo produto |
| D-14 | "Teste não age" decidido **no motor** (1.19.0) | ferramenta de efeito (e-mail, webhook, WhatsApp) não pode depender do desenho nem da própria ferramenta para não disparar num teste |
| D-15 | Falha de nó vira **resultado**, não exceção (1.19.0) | quem chama precisa fechar o run faturado e mostrar o rastro até o nó que falhou |
| D-16 | `sql_allowlist` **fail-closed** (1.17.0) | SQL que o `sqlglot` não consegue ler é recusado — "não sei, então não", como D-04 |

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
- **Contexto que não atravessa thread** (1.19.0) — produto em escopo e execução
  vivem em `ContextVar`; thread aberta pelo produto sem `copy_context()` grava
  no produto da config, **em silêncio** (a linha existe, o dono está errado).
- **`FlowRunner` síncrono** — ramos em threads, sem `async` (`LIB-39`); numa
  rota FastAPI precisa de `run_in_threadpool` para não travar o loop.
- **Corte de entrada silencioso** — `max_chars_entrada` (12.000) corta o que
  entra num agente sem aviso estruturado; documento longo chega truncado
  (`LIB-42`, `FG-56` do Forge).
- **Instrução do nó perde para o prompt do agente** — a montagem atual
  (instrução no fim do `system`) foi medida 9/15 no Forge (`LIB-41`).
