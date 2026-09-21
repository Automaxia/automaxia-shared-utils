# CHANGELOG — automaxia-shared-utils

Formato: [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) ·
Versionamento: [SemVer](https://semver.org/lang/pt-BR/).

Histórico anterior à 1.13.0 está na tabela de versões de
[`docs/SPEC.md`](docs/SPEC.md) §12.

---

## [1.17.0] — 2026-09-17

Responsável técnico: Wesley Romualdo da Silva

### Adicionado

- **Allowlist de tabelas por conexão** (`automaxia_utils.sql_allowlist`). A
  conexão do cofre passa a carregar `allowed_tables` (AdminCenter, migration
  0053), entregue no `ResolvedConnection.allowed_tables` que já existia.
  - `verificar_sql(sql, allowlist, dialeto, schema_padrao, catalogo_padrao)`
    levanta `TabelaNaoLiberada` se o SQL ler objeto fora da lista — JOIN,
    subconsulta, CTE e UNION incluídos. Sem allowlist não faz nada; SQL que não
    dá para ler é **recusado** (fail-closed).
  - `filtrar_tabelas(...)` / `liberada(...)` para a listagem de schema.
  - Itens `schema.objeto`, `schema.*` ou `objeto` (schema padrão); BigQuery
    `dataset.tabela` (projeto de fora nunca casa). Catálogo do Postgres
    (`information_schema`, `pg_catalog` e `pg_*` sem schema) é permitido.
- Dependência nova no core: `sqlglot` (puro Python).

Portado do ecossistema-irmão Balance (`infrabalance-utils` 2.11.1), já com a
correção do `pg_*` sem schema — a 2.11.0 de lá fazia o satélite recusar a
própria leitura do schema.

## [1.16.0] — 2026-09-15

### Adicionado

- **Engine `bigquery` no cofre.** O `ResolvedConnection` passa a entender a
  família BigQuery — `database_name` é o projeto, `schema_name` é o dataset,
  `password` é o JSON da service account e `username` é o `client_email`.
  `dsn()` devolve `bigquery://projeto/dataset` **sem credencial na URL**: a
  chave vai pelo `credentials_info` do dialeto, em `get_engine()`.

- **`get_bigquery_client(alias, maximum_bytes_billed=…)`** (e o equivalente no
  `AdminCenterService`), com `build_bigquery_client` exposto para quem já
  resolve a conexão por conta própria. O BigQuery **cobra por byte lido e o
  `LIMIT` não reduz a varredura**, então toda consulta leva um teto: o
  parâmetro ou a env `BIGQUERY_MAXIMUM_BYTES_BILLED`. Acima dele a consulta
  falha em vez de gerar a conta.

- Extra `[bigquery]` (`google-cloud-bigquery`, `sqlalchemy-bigquery`). Sem ele,
  `get_engine`/`get_bigquery_client` explicam o que instalar em vez de estourar
  `ImportError` cru.

### Corrigido

- `get_psycopg2()` numa conexão BigQuery recusava com "psycopg2 nao instalado"
  quando o driver faltava, escondendo o motivo real. A checagem de engine agora
  vem antes do import e diz o que usar (`get_engine`/`get_bigquery_client`).

---

## [1.15.1] — 2026-09-04

### Corrigido

- **Recusa de escrita deixava de contar como perda.** O `admincenter-api`
  responde num envelope `{success, data, message}` e as rotas POST de log
  declaram `status_code=201` no decorator — uma rejeição (`success: false`,
  "Produto nao encontrado.") chegava como **HTTP 201**. `_make_request` só olha
  o código HTTP, então o `_process_batch` contabilizava "1/1 enviados" para uma
  linha que nunca existiu, e o produto não tinha como saber. O batch worker
  agora inspeciona o envelope (`_envelope_aceito`) e loga a recusa em
  **WARNING** — antes até a falha de rede saía em `debug`, invisível para quem
  não estava justamente procurando por ela.

  Foi o que escondeu, por dias, a folha de pagamento (produto `ischolar`)
  gravando **zero** `application_logs`: a api-key pertencia a uma organização
  diferente da do produto e o guard de tenant do `admincenter-api` recusava
  cada POST — em silêncio, dos dois lados.

---

## [1.15.0] — 2026-08-31

Execução observável: dá para saber **o que** um agente fez, não só quanto ele
custou. Nada aqui quebra assinatura existente — todos os parâmetros novos são
opcionais.

### Adicionado

- **`agent_step(agent_slug, label=…)`** — context manager que marca uma etapa do
  trabalho de um agente e grava a linha do tempo em
  `admincenter.execution_steps` (migration 0044 do `admincenter-api`):

  ```python
  with admin.agent_step('harvest-mapeador-sql',
                        label='mapeando o array de entrada') as passo:
      passo.progress(40, 'validando colunas contra o schema')
      resultado = mapear()
      passo.done(tokens_in=pt, tokens_out=ct)
  ```

  Resolve sozinho: o `agent_id` **e o modelo EFETIVO** do slug (pelo mesmo
  `_resolve_agent_info` cacheado do token tracking — então a etapa grava o
  modelo que de fato rodou, não o `OPENAI_MODEL` do `.env`); o `run_id`
  herdado do run context dentro de um job; a duração; e o `status` final —
  `failed` quando a exceção sobe, **e ela continua subindo**.

  `stream=True` (padrão) grava uma linha `running` na entrada, para etapa
  travada ficar visível enquanto ainda está travada. `stream=False` grava só a
  linha final.
- **`execution_scope()`** — agrupa os passos de uma execução lógica que não vem
  de job (pipeline síncrono de request HTTP). Reentrante; escopo aninhado não
  rouba a correlação do de fora.
- **`log_step(...)`** — nível baixo, para quem não quer o context manager.
- **`log_process(agent_slug=, area_agent_slug=, connection_id=)`** — a execução
  passa a carregar QUEM executou e sobre QUAL base (migration 0043). Resolve os
  slugs pelo mesmo cache do token tracking, de propósito: `token_usage` e
  `process_execution_logs` apontam para o mesmo `agents.id`, então o JOIN que
  responde "o agente X gastou 40k tokens fazendo o quê?" fica direto. Antes
  dava para responder só a primeira metade.

### Melhorado

- **Passo custa 1 POST, não N.** `_process_batch` agrupa todos os itens
  `execution_step` da fila num único POST para `/logs/step`, que recebe lista.
  Um pipeline de 16 etapas vira 1 requisição, não 32 — sem isso, a
  granularidade fina que a tabela existe para permitir sairia cara justamente
  onde ela mais serve.
- `_make_request` passa a tipar `data` como `Any`: `/logs/step` manda lista.

### Nota de desenho

Passo **não** vai para `process_execution_logs`. Aquela tabela é o RUN, e o
relatório de faturamento decide o que cobra pelo **nome** do processo
(`billing-report.service.ts::isBillable`, que quebra `process_name` por ponto e
não consulta `parent_execution_id`). Gravar etapa como run multiplicaria a
fatura do cliente pelo número de passos do pipeline — sem nenhum teste pegar.

---

## [1.14.0] — 2026-08-26

Sincronização com a `infrabalance-shared-utils` (Balance, 2.9.0). Cada item
abaixo cobre uma **falha silenciosa** — nada disso levantava erro; o produto
seguia funcionando com o dado faltando. Nenhuma mudança quebra assinatura
existente. `ingest/` **não** foi portado (domínio de saneamento).

### Corrigido

- **Gate de produto era pulado em silêncio.** `AdminCenterAuthConfig.from_env`
  lia apenas `PRODUCT_SLUG`, mas os quatro satélites do Studio definem
  `ADMIN_CENTER_PRODUCT_SLUG` no `.env`. Com `product_slug=""`, o
  `if slug and …` de `require_product_access()` não entrava e **qualquer JWT
  válido acessava qualquer produto**. Passa a aceitar as duas vars (prefixada
  vence).
- **Auth em dev batia em produção.** O mesmo `from_env` ignorava
  `ADMIN_CENTER_URL_LOCAL`/`ADMIN_CENTER_DEV_URL` mesmo com
  `ENVIRONMENT=development`. O `/auth/me/full` que enriquece permissões ia para
  o AdminCenter de produção carregando um token emitido pelo local — o
  `user_id` não existe lá, e todo `require_permission` respondia 403/503 com a
  permissão devidamente concedida no banco local. Agora segue a mesma
  precedência de `registration/client.py` e do `AdminCenterConfig`.
- **Conexão `rest`/`arcgis` não resolvia.** `ResolvedConnection.from_dict` lia
  `data["host"]`/`["port"]`/`["database_name"]` como obrigatórios; para engines
  HTTP esses campos não vêm no payload e o `KeyError` era reportado pelo
  `_fetch` como "payload inválido" → `None`. O produto exibia "conexão não
  encontrada ou sem permissão" para uma conexão que existia e estava liberada.
  Os três viraram opcionais.
- **`get_variable()` chamava `/environment/None/variables`.** Sem
  `environment_id` resolvido, montava a URL com `None` e o produto subia sem
  nenhuma variável, deixando só um 404 solto no log. Agora tenta a descoberta
  por slug e, falhando, avisa e retorna `None` sem tocar a rede.
- **Campos do cofre eram descartados no caminho.** O `/resolve` já devolve
  `base_url`, `auth_type`, `databricks_http_path`, `arcgis_config`,
  `ai_context`, `events_table` e `operational_table` (migration 0042), mas o
  DTO da lib não tinha esses atributos. Os satélites leem via
  `getattr(resolved, campo, None)` — campo ausente não dá erro, cai no default
  e o produto degrada em silêncio. Todos foram adicionados.
- **Tracking de token da LangChain caía na estimativa.** Quem usa
  `llm.invoke()` recebe um `AIMessage`, não o response cru da API.
  `extract_tokens_from_response` não olhava `response_metadata.token_usage`
  nem `usage_metadata`, então o custo gravado vinha do tiktoken mesmo com o
  número exato em mãos.
- **Logs sem identidade viravam 422 mudo.** `log_application`, `log_execution`,
  `log_process` e o token usage enviavam `product_id=""` quando os ids não
  estavam configurados — o contrato tipa como UUID e recusava o lote inteiro,
  do outro lado. Passam a checar `has_logging_identity()` antes de enfileirar
  (o método já existia; ninguém o chamava).
- **`context` do log morria na porta de entrada.** `ApplicationLogPOST` não tem
  campo `context`, e o Pydantic descarta chave desconhecida sem reclamar — o
  dict inteiro sumia. Agora é DESMEMBRADO: `logger`, `module`,
  `funcName`/`function`, `lineno`/`line` viram as colunas correspondentes
  (`function`/`line` são os apelidos que o interceptor do `talk-api` emite), e
  o que sobra vai para `extra_data`. Parâmetro explícito vence o `context`.
  `line_number` não numérico vai para `extra_data` em vez de derrubar o lote.

### Adicionado

- **`AdminCenterConfig.log_min_level`** (`ADMIN_CENTER_LOG_MIN_LEVEL`, default
  `WARNING`). `application_logs` virou rastro de execução: a maior parte das
  linhas é INFO de processo, que já existe no stdout do pod. O descarte é
  **antes da fila** — não gasta rede, lote nem linha no banco. Não mexe no
  logging local. Para depurar um produto em janela curta, baixe para `INFO` na
  variável do produto no AdminCenter e volte depois.
  ⚠️ **Mudança de comportamento:** INFO/DEBUG deixam de ser persistidos por
  default.
- **`AdminCenterConfig.product_slug`** (`ADMIN_CENTER_PRODUCT_SLUG` ou
  `PRODUCT_SLUG`) e **`_discover_environment_id_via_api()`**: com slug +
  api-key, a lib resolve `product_id` e `environment_id` via
  `/product/consulta_slug` + `/product/{id}/environment` e os espelha no
  `config` — `has_logging_identity()` lê do config, e sem espelhar o guard
  ficaria `False` mesmo com o ambiente já descoberto.
- **Metadados da api-key** expostos no service: `api_key_mode`,
  `api_key_scopes`, `api_key_prefix`, `api_key_organization_id` — evitam que o
  produto reabra o JWT para saber em que silo está.
- **`log_process(execution_id=…)`** → `request_id` do backend. Correlaciona o
  `started` e o `completed`/`failed` do mesmo ciclo; sem ele o par início↔fim
  só podia ser adivinhado por proximidade no tempo, que erra exatamente quando
  duas execuções se sobrepõem — que é quando ler o log importa. Aceita `UUID`
  ou string; valor inválido é descartado com aviso (telemetria não derruba nem
  cala o log do produto).
- **`ResolvedConnection.dsn()`** levanta `ValueError` nomeado para
  `rest`/`arcgis`/`databricks` e para SQL incompleto, em vez de montar um DSN
  com `None:None` e deixar o driver falhar.
- **`AdminCenterEndpoints` reexportado** na raiz do pacote.
- 39 testes novos em `tests/test_sync_balance_1_14.py`.

### Dimensões de custo por agente (a lacuna que restava)

O `counter.py` não preenchia **nenhuma** das duas colunas de agente — só
gravava `agent_slug` dentro do `metadata`. O ranking de custo por agente ficava
vazio. O Balance resolve mandando slugs e deixando o Cockpit traduzir na
escrita (migrations 034/035); o `admincenter-api` do Studio só aceita UUIDs
(migration 0036) e não traduz slug. **A tradução passou a ser da lib**, que já
tinha o caminho pronto:

- **`AdminCenterService.resolve_agent_id(slug)`** — traduz slug → `agents.id`
  pelo `effective-prompt`, que já devolve `agent_id` e já era chamado para
  resolver o modelo. Zero endpoint novo, zero roundtrip extra no caso comum.
- **`track_token_usage(agent_slug=…, area_agent_slug=…)`** — resolve as duas
  dimensões. Id explícito vence o slug. Slug que não resolve **omite a coluna**
  (são FK para `agents.id`; id inventado derrubaria o lote inteiro) mas
  sobrevive no `alert_metadata`, que é o que deixa o rastro legível quando o
  agente não está vinculado ao produto.
- **`track_api_response`** volta a separar **ETAPA** (o `agent_slug` do call
  site — roteador, gerador de SQL, validador) de **ÁREA** (o ContextVar de
  `definir_agente()`, marcado uma vez na entrada do endpoint). Sem etapa
  explícita, a etapa é a própria área — quem só marca na entrada não perde
  atribuição. O `LangChainTokenCallback` herda a área do mesmo jeito.

Cuidado que o caminho quente exigiu: `_resolve_agent_info` cacheia o
`agent_id` **sempre** (identidade é estável), o modelo só quando resolvido
(para pegar sozinho a configuração feita depois no painel) e **a negativa por
5 min**. Sem cachear a negativa, um slug que não resolve — agente não
vinculado, typo no call site, AdminCenter oscilando — somaria um roundtrip HTTP
a *cada* chamada de LLM, indefinidamente. Telemetria não pode custar mais que o
trabalho que ela mede.

17 testes em `tests/test_dimensoes_agente.py`.

### Não portado (deliberadamente)

- **`ingest/`** — bridge do Agente Comercial, domínio de saneamento
  (ver `CLAUDE.md` do ecossistema, §6).
- **Tradução slug → id no servidor** (o desenho do Cockpit) — resolvida na lib,
  como acima. O `admincenter-api` segue recebendo só UUID.
