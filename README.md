# Automaxia Utils v1.1.0

Pacote compartilhado para rastreamento de tokens de IA, gerenciamento de prompts centralizados e integracao com Admin Center API.

## Instalacao

### Via Git (Recomendado)

```bash
# Instalar basico
pip install git+https://github.com/automaxia/automaxia-shared-utils.git

# Com suporte a LangChain
pip install "automaxia-utils[langchain] @ git+https://github.com/automaxia/automaxia-shared-utils.git"

# Com APIs nativas de providers (Anthropic, Google)
pip install "automaxia-utils[providers] @ git+https://github.com/automaxia/automaxia-shared-utils.git"

# Tudo incluso
pip install "automaxia-utils[all] @ git+https://github.com/automaxia/automaxia-shared-utils.git"
```

### Instalacao Local (Desenvolvimento)

```bash
cd automaxia-shared-utils
pip install -e .             # basico
pip install -e ".[all]"      # tudo
pip install -e ".[dev]"      # com ferramentas de teste
```

## Configuracao

Crie um arquivo `.env` no seu projeto:

```env
# Obrigatorio
ADMIN_CENTER_URL=https://admincenter-api.automaxia.com.br/api
ADMIN_CENTER_API_KEY=sua-api-key
ADMIN_CENTER_PRODUCT_ID=uuid-do-produto
ADMIN_CENTER_ENVIRONMENT_ID=uuid-do-ambiente
ADMIN_CENTER_ORGANIZATION_ID=uuid-da-organizacao

# Opcional
ADMIN_CENTER_ENABLED=true
ADMIN_CENTER_BATCH_MODE=true
ADMIN_CENTER_BATCH_SIZE=50
ADMIN_CENTER_BATCH_INTERVAL=2
ADMIN_CENTER_TIMEOUT=10
ENVIRONMENT=production
```

## Arquitetura

### Contagem de Tokens (5 niveis de precisao)

```
Nivel 1: response.usage (da API)     -> 100% exato
Nivel 2: LiteLLM token_counter       -> universal, 100+ modelos
Nivel 3: Anthropic/Google nativo     -> exato por provider
Nivel 4: tiktoken                    -> fallback offline (OpenAI)
Nivel 5: len(text) // 4              -> ultimo recurso
```

### Calculo de Custos (3 niveis)

```
Nivel 1: LiteLLM cost_per_token      -> atualizado pela comunidade
Nivel 2: Admin Center API            -> precos cadastrados no painel
Nivel 3: Fallback hardcoded          -> precos Mar 2026
```

### Providers Suportados

| Provider | Contagem | Custos | Extracao de tokens |
|----------|----------|--------|-------------------|
| OpenAI (GPT-4o, GPT-4, GPT-3.5) | LiteLLM + tiktoken | LiteLLM + API | response.usage |
| Anthropic (Claude 3.5/4) | LiteLLM + nativo | LiteLLM + API | response.usage |
| Google (Gemini 1.5/2.0) | LiteLLM + nativo | LiteLLM + API | usage_metadata |
| LangChain | callback | LiteLLM + API | llm_output |
| Outros (100+ via LiteLLM) | LiteLLM | LiteLLM | auto-detect |

---

## Uso

### 1. Tracking de Tokens (funcao principal)

```python
from automaxia_utils import track_api_response
from openai import OpenAI

client = OpenAI()
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Ola!"}]
)

# Rastrear tokens e custos automaticamente
tracking = track_api_response(
    response=response,
    model="gpt-4o",
    endpoint="/chat/hello",
    user_id="user_123"
)

print(f"Tokens: {tracking['total_tokens']}")
print(f"Custo: ${tracking['cost_usd']:.6f} / R${tracking['cost_brl']:.4f}")
print(f"Fonte: {tracking['source']}")         # openai_api
print(f"Precos: {tracking['price_source']}")  # litellm
```

Funciona com qualquer provider:

```python
# Anthropic
from anthropic import Anthropic
client = Anthropic()
response = client.messages.create(model="claude-sonnet-4-20250514", ...)
tracking = track_api_response(response=response, model="claude-sonnet-4-20250514")

# Google Gemini
import google.generativeai as genai
model = genai.GenerativeModel("gemini-1.5-pro")
response = model.generate_content("Ola!")
tracking = track_api_response(response=response, model="gemini-1.5-pro")
```

### 2. Prompts Centralizados (NOVO v1.1.0)

Busque prompts cadastrados no Admin Center ao inves de manter hardcoded no codigo:

```python
from automaxia_utils import get_admin_center_service

admin = get_admin_center_service()

# Buscar prompt por slug
prompt = admin.get_prompt("datachat-sql-agent")
print(prompt["content"])       # conteudo do prompt
print(prompt["temperature"])   # 0.2
print(prompt["max_tokens"])    # 2000
print(prompt["tags"])          # ['analise', 'geracao']

# Listar todos os prompts do produto
prompts = admin.get_prompts()
for p in prompts:
    print(f"{p['name']} ({p['slug']}) - v{p['version']}")

# Filtrar por tags
prompts_analise = admin.get_prompts(tags=["analise"])
```

### 3. Tracking vinculado a Prompt (NOVO v1.1.0)

Vincule o uso de tokens ao prompt que originou a chamada:

```python
from automaxia_utils import get_admin_center_service, track_api_response
from openai import OpenAI

admin = get_admin_center_service()
client = OpenAI()

# 1. Buscar prompt centralizado
prompt_data = admin.get_prompt("datachat-sql-agent")

# 2. Substituir variaveis
content = prompt_data["content"]
content = content.replace("{schema_context}", "tabela: users (id, name, email)")
content = content.replace("{user_question}", "Quantos usuarios ativos?")

# 3. Chamar LLM com o prompt
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "system", "content": content}],
    temperature=prompt_data["temperature"],
    max_tokens=prompt_data["max_tokens"]
)

# 4. Trackear vinculado ao prompt
tracking = track_api_response(
    response=response,
    model="gpt-4o-mini",
    prompt_id=prompt_data["id"],      # vincula ao prompt
    prompt_text=content,
    endpoint="/sql-agent"
)
# Automaticamente registra no prompt_usage_logs do AdminCenter
```

### 4. Estimativa de Custos (antes da chamada)

```python
from automaxia_utils import estimate_tokens_and_cost

estimativa = estimate_tokens_and_cost(
    prompt="Explique machine learning em 3 paragrafos",
    model="gpt-4o",
    estimated_response_length=300
)

print(f"Tokens estimados: {estimativa['estimated_total_tokens']}")
print(f"Custo estimado: ${estimativa['cost_usd']:.6f}")
```

### 5. Prompt Efetivo por Agente (NOVO v1.1.0)

Resolve o prompt efetivo de um agente, combinando prompts genericos e customizacoes do produto:

```python
from automaxia_utils import get_admin_center_service

admin = get_admin_center_service()

# Buscar prompt efetivo do agente
ep = admin.get_effective_prompt("sql-analyst")

# Montar system message com conteudo generico + customizacao
system_parts = [ep["generic_content"]]
if ep.get("custom_content"):
    system_parts.append(ep["custom_content"])
system_message = "\n\n---\n\n".join(system_parts)

# Campos disponiveis:
# ep["generic_content"]              -> str: prompts base concatenados
# ep["generic_prompts"]              -> list: prompts individuais (id, name, content)
# ep["generic_temperature"]          -> float
# ep["generic_max_tokens"]           -> int
# ep["custom_content"]               -> str | None: instrucao adicional do produto
# ep["is_customized"]                -> bool
# ep["is_prompt_selection_active"]   -> bool
# ep["selected_prompt_ids"]          -> list[str]

# Tambem aceita product_id explicito
ep = admin.get_effective_prompt("sql-analyst", product_id="uuid-do-produto")
```

### 6. Admin Center Service

```python
from automaxia_utils import get_admin_center_service

admin = get_admin_center_service()

# Variaveis de ambiente
variaveis = admin.get_variable()

# Secrets (descriptografados)
api_key = admin.get_secret("OPENAI_API_KEY")

# Log de processo
admin.log_process("import_data", "started", metadata={"source": "csv"})
admin.log_process("import_data", "completed", duration_ms=1500)

# Log de aplicacao
admin.log_application("error", "Falha na conexao", context={"host": "db.local"})

# Log de execucao HTTP
admin.log_execution("/api/users", "GET", 200, response_time_ms=45)

# Log de uso de prompt (com parametros opcionais)
admin.log_prompt_usage(
    prompt_id="uuid-do-prompt",
    variables_used={"empresa": "CASAN", "area": "saneamento"},
    final_prompt="Voce e um assistente de CASAN...",
    tokens_used=1500,
    model_used="gpt-4o",
    product_id="uuid-do-produto",         # opcional, usa .env se omitido
    environment_id="uuid-do-ambiente",     # opcional, usa .env se omitido
    request_id="uuid-do-request"           # opcional, auto-gerado se omitido
)

# Invalidar cache de modelos
admin.invalidate_model_cache("gpt-4o")    # modelo especifico
admin.invalidate_model_cache()             # todo o cache
```

### 7. Decorator para Tracking Automatico

```python
from automaxia_utils import track_execution

@track_execution(process_name="processar_pedido")
def processar_pedido(pedido_id):
    # Automaticamente loga inicio, fim, duracao e erros
    resultado = fazer_processamento(pedido_id)
    return resultado
```

### 8. LangChain Integration

```python
from automaxia_utils import LangChainTokenCallback
from langchain.llms import OpenAI

callback = LangChainTokenCallback(model="gpt-4o", endpoint="/langchain/query")
llm = OpenAI(callbacks=[callback])
# Tokens sao automaticamente rastreados
```

### 9. Context Manager

```python
from automaxia_utils import AdminCenterContext

with AdminCenterContext() as admin:
    admin.log_process("batch_job", "started")
    # ... processamento ...
    admin.log_process("batch_job", "completed")
# Flush automatico ao sair
```

---

## Estrutura de Resposta

### track_api_response()

```python
{
    "prompt_tokens": 50,
    "completion_tokens": 100,
    "total_tokens": 150,
    "source": "openai_api",           # openai_api, litellm_fallback, tiktoken, etc
    "provider": "openai",             # openai, anthropic, google, langchain, unknown
    "model": "gpt-4o",
    "prompt_id": "uuid-ou-none",      # NOVO v1.1.0
    "admin_center_tracked": true,
    "timestamp": "2026-03-17T10:30:00",
    "cost_usd": 0.0045,
    "cost_brl": 22.50,
    "exchange_rate": 5.0,
    "price_source": "litellm",        # litellm, admin_center_api, fallback_hardcoded
    "cost_breakdown": {
        "input_usd": 0.0015,
        "output_usd": 0.0030,
        "input_brl": 7.50,
        "output_brl": 15.00
    }
}
```

---

## API Reference

### Funcoes Principais

| Funcao | Descricao |
|--------|-----------|
| `track_api_response(response, model, ...)` | Tracking universal - detecta provider automaticamente. Params extras: `prompt_text`, `prompt_id`, `force_provider` |
| `estimate_tokens_and_cost(prompt, model)` | Estimativa previa de tokens e custos |
| `count_tokens_smart(text, model)` | Contagem inteligente multi-nivel |
| `count_tokens_litellm(text, model)` | Contagem via LiteLLM (100+ modelos) |
| `count_tokens_tiktoken(text, model)` | Contagem via tiktoken (OpenAI) |
| `get_admin_center_service()` | Singleton do AdminCenterService |

### Metodos do AdminCenterService

| Metodo | Descricao |
|--------|-----------|
| `get_prompt(slug)` | Busca prompt por slug |
| `get_prompt_by_id(prompt_id)` | Busca prompt por UUID |
| `get_prompts(product_id, tags)` | Lista prompts do produto |
| `log_prompt_usage(prompt_id, ...)` | Registra uso de prompt |
| `get_variable()` | Busca variaveis de ambiente |
| `get_secret(name)` | Busca e descriptografa secret |
| `track_token_usage(model, tokens, ...)` | Registra uso de tokens |
| `log_application(level, message)` | Log de aplicacao |
| `log_execution(endpoint, method, ...)` | Log de execucao HTTP |
| `log_process(name, status, ...)` | Log de processo |
| `get_effective_prompt(agent_slug, product_id)` | Resolve prompt efetivo do agente (generico + custom) |
| `invalidate_model_cache(model_name)` | Invalida cache de modelo especifico ou todo o cache |
| `flush()` | Forca envio de items pendentes |
| `shutdown()` | Finaliza o servico |

---

## Migracao de v1.0.x para v1.1.0

**Nao precisa alterar nada.** Todas as funcoes existentes mantem a mesma assinatura.

Mudancas sao apenas adicoes:
- `prompt_id` adicionado como parametro opcional (default `None`) em `track_api_response()` e `track_token_usage()`
- Novos metodos: `get_prompt()`, `get_prompts()`, `log_prompt_usage()`
- LiteLLM adicionado como dependencia (melhora precisao automaticamente)
- Precos fallback atualizados para modelos de 2026

Basta reinstalar:

```bash
pip install --upgrade git+https://github.com/automaxia/automaxia-shared-utils.git
```

---

## Estrutura do Projeto

```
automaxia-shared-utils/
├── automaxia_utils/
│   ├── __init__.py                    # Exports principais (v1.1.0)
│   ├── admin_center/
│   │   ├── __init__.py
│   │   └── service.py                 # AdminCenterService + Prompts
│   ├── token_tracking/
│   │   ├── __init__.py
│   │   └── counter.py                 # Contagem multi-nivel + LiteLLM
│   └── config/
│       └── settings.py
├── tests/
├── setup.py                           # v1.1.0
├── requirements.txt
└── README.md
```

## Dependencias

| Pacote | Versao | Obrigatorio | Para que |
|--------|--------|-------------|----------|
| requests | >=2.32.0 | Sim | HTTP client |
| python-decouple | >=3.8 | Sim | Variaveis de ambiente |
| tiktoken | >=0.7.0 | Sim | Tokenizer offline (OpenAI) |
| litellm | >=1.40.0 | Sim | Contagem universal + custos |
| langchain | >=0.1.0 | Nao | `pip install .[langchain]` |
| anthropic | >=0.25.0 | Nao | `pip install .[providers]` |
| google-generativeai | >=0.5.0 | Nao | `pip install .[providers]` |

## Troubleshooting

### Tokens nao batem com dashboard do provider

Sempre passe o objeto completo da resposta:

```python
# Errado - usa tiktoken (estimativa)
tracking = track_openai_call(prompt_text, response_text, model)

# Correto - usa response.usage (exato)
tracking = track_api_response(response=response_object, model=model)
```

### Admin Center nao envia dados

```python
import logging
logging.basicConfig(level=logging.DEBUG)

from automaxia_utils import get_admin_center_service
admin = get_admin_center_service()
print(f"Habilitado: {admin.config.enabled}")
print(f"Valido: {admin.config.is_valid()}")
print(f"URL: {admin.config.api_url}")
```

### Prompt nao encontrado

```python
prompt = admin.get_prompt("meu-slug")
if prompt is None:
    # Verificar: slug existe? produto correto? prompt ativo?
    prompts = admin.get_prompts()
    for p in prompts:
        print(f"{p['slug']} - ativo: {p['is_active']}")
```

## Changelog

### v1.1.0 (2026-03-17)
- Contagem de tokens multi-nivel (LiteLLM + APIs nativas + tiktoken)
- Calculo de custos via LiteLLM (100+ modelos atualizados)
- Gerenciamento de prompts centralizados (get_prompt, get_prompts, log_prompt_usage)
- Prompt efetivo por agente (get_effective_prompt) com suporte a customizacao por produto
- Parametro prompt_id para vincular tracking ao prompt
- Parametros force_provider e prompt_text em track_api_response()
- Invalidacao de cache de modelos (invalidate_model_cache)
- Suporte a Google Gemini (contagem + extracao)
- Precos fallback atualizados (GPT-4o, Claude 4, Gemini 2.0)
- Extras de instalacao: [providers], [all]

### v1.0.0 (2025-01-15)
- Extracao de tokens da resposta da API
- Integracao com Admin Center Service
- Calculo de custos USD/BRL
- Batch processing assincrono
- Suporte a OpenAI, Anthropic e LangChain

## Licenca

MIT License
