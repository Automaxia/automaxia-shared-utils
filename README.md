# Automaxia Utils

Pacote compartilhado para rastreamento de tokens de IA e integração com Admin Center API.

## 🚀 Instalação

### Opção 1: Via Git (Recomendado)

```bash
# Instalar diretamente do repositório
pip install git+https://github.com/automaxia/automaxia-utils.git

# Instalar versão específica
pip install git+https://github.com/automaxia/automaxia-utils.git@v1.0.0
```

### Opção 2: Instalação Local (Desenvolvimento)

```bash
# Clonar o repositório
git clone https://github.com/automaxia/automaxia-utils.git
cd automaxia-utils

# Instalar em modo editável
pip install -e .

# Instalar com dependências de desenvolvimento
pip install -e ".[dev]"

# Instalar com suporte a LangChain
pip install -e ".[langchain]"
```

## ⚙️ Configuração

### Variáveis de Ambiente

Crie um arquivo `.env` no seu projeto:

```env
# Admin Center - Obrigatório
ADMIN_CENTER_URL=https://api.admincenter.com
ADMIN_CENTER_API_KEY=sua-api-key-aqui
ADMIN_CENTER_PRODUCT_ID=seu-product-id
ADMIN_CENTER_ENVIRONMENT_ID=seu-environment-id
ADMIN_CENTER_ORGANIZATION_ID=sua-organization-id

# Admin Center - Opcional
ADMIN_CENTER_ENABLED=true
ADMIN_CENTER_BATCH_MODE=true
ADMIN_CENTER_BATCH_SIZE=50
ADMIN_CENTER_BATCH_INTERVAL=2
ADMIN_CENTER_TIMEOUT=10

# Ambiente
ENVIRONMENT=production  # ou development
```

## 📖 Uso Básico

### 1. Rastreamento de Tokens (OpenAI)

```python
from automaxia_utils import track_api_response
from openai import OpenAI

client = OpenAI()

# Fazer chamada à API
response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Olá!"}]
)

# Rastrear tokens e custos
tracking = track_api_response(
    response=response,        # Objeto completo da resposta
    model="gpt-4",
    endpoint="/chat/hello",
    user_id="user_123"
)

print(f"Tokens: {tracking['total_tokens']}")
print(f"Custo USD: ${tracking['cost_usd']:.6f}")
print(f"Custo BRL: R${tracking['cost_brl']:.4f}")
```

### 2. Estimativa de Custos (Antes da Chamada)

```python
from automaxia_utils import estimate_tokens_and_cost

prompt = "Explique inteligência artificial em 3 parágrafos"

estimativa = estimate_tokens_and_cost(
    prompt=prompt,
    model="gpt-4",
    estimated_response_length=300
)

print(f"Custo estimado: ${estimativa['cost_usd']:.6f}")
```

### 3. Admin Center Direto

```python
from automaxia_utils import get_admin_center_service

admin = get_admin_center_service()

# Buscar variável de ambiente
valor = admin.get_variable("MINHA_CONFIG")

# Buscar secret
api_key = admin.get_secret("OPENAI_API_KEY")

# Log de processo
admin.log_process(
    process_name="import_data",
    status="started"
)
```

### 4. Decorator para Tracking Automático

```python
from automaxia_utils import track_execution

@track_execution(process_name="processar_pedido")
def processar_pedido(pedido_id):
    # Seu código aqui
    resultado = fazer_processamento(pedido_id)
    return resultado

# Automaticamente loga início, fim, duração e erros
```

### 5. LangChain Integration

```python
from automaxia_utils import LangChainTokenCallback
from langchain.llms import OpenAI
from langchain.chains import LLMChain

# Criar callback
callback = LangChainTokenCallback(
    model="gpt-3.5-turbo",
    endpoint="/langchain/query"
)

# Usar em chains
llm = OpenAI(callbacks=[callback])
chain = LLMChain(llm=llm, prompt=prompt_template)
resultado = chain.run(input_data)

# Tokens são automaticamente rastreados
```

## 🎯 Exemplos Avançados

### Múltiplos Projetos com Configurações Diferentes

```python
from automaxia_utils import AdminCenterConfig, AdminCenterService

# Projeto 1 - Configuração personalizada
config_projeto1 = AdminCenterConfig(
    api_url="https://api.admincenter.com",
    api_key="key-projeto-1",
    product_id="produto-1",
    environment_id="env-1",
    batch_size=100
)

admin1 = AdminCenterService(config_projeto1)

# Projeto 2 - Usar variáveis de ambiente
from automaxia_utils import get_admin_center_service
admin2 = get_admin_center_service()  # Lê do .env
```

### Context Manager para Cleanup Automático

```python
from automaxia_utils import AdminCenterContext

with AdminCenterContext() as admin:
    admin.log_process("batch_job", "started")
    # Fazer processamento
    admin.log_process("batch_job", "completed")
    
# Flush automático ao sair do contexto
```

### Invalidar Cache de Preços

```python
from automaxia_utils import invalidate_model_price_cache

# Após atualizar preços na API do Admin Center
invalidate_model_price_cache("gpt-4")

# Próxima chamada buscará preços atualizados
```

## 📊 Estrutura de Resposta

### track_api_response()

```python
{
    "prompt_tokens": 50,
    "completion_tokens": 100,
    "total_tokens": 150,
    "source": "api_response",  # ou "tiktoken_fallback"
    "model": "gpt-4",
    "admin_center_tracked": True,
    "timestamp": "2025-01-15T10:30:00",
    "cost_usd": 0.0045,
    "cost_brl": 22.50,
    "exchange_rate": 5.0,
    "price_source": "api",  # ou "fallback"
    "cost_breakdown": {
        "input_usd": 0.0015,
        "output_usd": 0.0030,
        "input_brl": 7.50,
        "output_brl": 15.00
    }
}
```

## 🔧 Troubleshooting

### Tokens não batem com OpenAI Dashboard

**Causa**: Usando `track_openai_call()` ao invés de `track_api_response()`

**Solução**: Sempre passar o objeto completo da resposta:

```python
# ❌ Incorreto
tracking = track_openai_call(prompt, response_text, model)

# ✅ Correto
tracking = track_api_response(response, model)
```

### Admin Center não está enviando dados

**Verificar**:
1. `ADMIN_CENTER_ENABLED=true` no `.env`
2. Credenciais corretas
3. Logs: `logging.basicConfig(level=logging.DEBUG)`

```python
import logging
logging.basicConfig(level=logging.DEBUG)

from automaxia_utils import get_admin_center_service
admin = get_admin_center_service()
print(f"Habilitado: {admin.config.enabled}")
print(f"Válido: {admin.config.is_valid()}")
```

### Preços diferentes da OpenAI

**Verificar**:
1. Modelo correto na chamada
2. Preços atualizados no Admin Center API
3. Fonte dos preços: `tracking['price_source']`

```python
# Forçar atualização de cache
from automaxia_utils import invalidate_model_price_cache
invalidate_model_price_cache()
```

## 🧪 Testes

```bash
# Executar testes
pytest

# Com cobertura
pytest --cov=automaxia_utils --cov-report=html

# Apenas testes específicos
pytest tests/test_token_counter.py
```

## 📦 Atualização

```bash
# Atualizar para última versão
pip install --upgrade git+https://github.com/automaxia/automaxia-utils.git

# Atualizar para versão específica
pip install --upgrade git+https://github.com/automaxia/automaxia-utils.git@v1.1.0
```

## 🤝 Contribuindo

1. Fork o repositório
2. Crie uma branch: `git checkout -b feature/nova-funcionalidade`
3. Commit: `git commit -m 'Add: nova funcionalidade'`
4. Push: `git push origin feature/nova-funcionalidade`
5. Abra um Pull Request

## 📝 Changelog

### v1.0.0 (2025-01-15)
- ✨ Extração precisa de tokens da resposta da API
- ✨ Integração com Admin Center Service
- ✨ Cálculo automático de custos USD/BRL
- ✨ Sistema assíncrono de batch processing
- ✨ Suporte a OpenAI, Anthropic e LangChain
- ✨ Cache inteligente de preços

## 📄 Licença

MIT License - ver arquivo LICENSE

## 🆘 Suporte

- Issues: https://github.com/automaxia/automaxia-utils/issues
- Email: dev@automaxia.com