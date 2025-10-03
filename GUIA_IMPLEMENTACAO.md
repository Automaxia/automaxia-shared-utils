# Guia de Implementação: Automaxia Utils

## 🎯 Objetivo

Criar um pacote Python reutilizável para ser usado em **todos os projetos** da Automaxia, eliminando duplicação de código.

---

## 📋 Passo 1: Criar Repositório do Pacote

### 1.1 Criar novo repositório Git

```bash
# Criar repositório (GitHub, GitLab, Bitbucket, etc)
# Nome sugerido: automaxia-utils

# Clonar localmente
git clone https://github.com/automaxia/automaxia-utils.git
cd automaxia-utils
```

### 1.2 Criar estrutura de diretórios

```bash
mkdir -p automaxia_utils/admin_center
mkdir -p automaxia_utils/token_tracking
mkdir -p automaxia_utils/config
mkdir -p tests
```

---

## 📋 Passo 2: Mover Arquivos Existentes

### 2.1 Estrutura final

```
automaxia-utils/
├── setup.py                                    # ← Criar (arquivo fornecido)
├── README.md                                   # ← Criar (arquivo fornecido)
├── requirements.txt                            # ← Criar
├── .env.example                               # ← Criar
├── .gitignore                                 # ← Criar
├── LICENSE                                    # ← Criar
└── automaxia_utils/
    ├── __init__.py                            # ← Criar (arquivo fornecido)
    ├── admin_center/
    │   ├── __init__.py                        # ← Criar
    │   └── service.py                         # ← Mover admin_center_service.py
    ├── token_tracking/
    │   ├── __init__.py                        # ← Criar
    │   └── counter.py                         # ← Mover token_counter.py
    └── config/
        ├── __init__.py                        # ← Criar
        └── settings.py                        # ← Criar (opcional)
```

### 2.2 Mover arquivos

```bash
# Copiar seus arquivos atuais
cp /caminho/do/seu/projeto/admin_center_service.py automaxia_utils/admin_center/service.py
cp /caminho/do/seu/projeto/token_counter.py automaxia_utils/token_tracking/counter.py
```

### 2.3 Ajustar imports dentro dos arquivos

**Em `automaxia_utils/token_tracking/counter.py`:**

```python
# ANTES:
from utils.admin_center_service import get_admin_center_service

# DEPOIS:
from automaxia_utils.admin_center.service import get_admin_center_service
```

---

## 📋 Passo 3: Criar Arquivos de Configuração

### 3.1 requirements.txt

```txt
requests>=2.31.0
python-decouple>=3.8
tiktoken>=0.5.1
```

### 3.2 .env.example

```env
# Admin Center - Obrigatório
ADMIN_CENTER_URL=https://api.admincenter.com
ADMIN_CENTER_API_KEY=your-api-key-here
ADMIN_CENTER_PRODUCT_ID=your-product-id
ADMIN_CENTER_ENVIRONMENT_ID=your-environment-id
ADMIN_CENTER_ORGANIZATION_ID=your-organization-id

# Admin Center - Opcional (valores padrão otimizados)
ADMIN_CENTER_ENABLED=true
ADMIN_CENTER_BATCH_MODE=true
ADMIN_CENTER_BATCH_SIZE=50
ADMIN_CENTER_BATCH_INTERVAL=2

# Ambiente
ENVIRONMENT=production
```

### 3.3 .gitignore

```
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# Virtual environments
venv/
ENV/
env/

# IDE
.vscode/
.idea/
*.swp
*.swo

# Environment
.env
.env.local

# Testing
.pytest_cache/
.coverage
htmlcov/
```

### 3.4 automaxia_utils/__init__.py files

**automaxia_utils/admin_center/__init__.py:**
```python
from .service import (
    AdminCenterService,
    AdminCenterConfig,
    get_admin_center_service,
    reset_admin_center_service,
    AdminCenterContext,
    track_execution
)

__all__ = [
    "AdminCenterService",
    "AdminCenterConfig",
    "get_admin_center_service",
    "reset_admin_center_service",
    "AdminCenterContext",
    "track_execution",
]
```

**automaxia_utils/token_tracking/__init__.py:**
```python
from .counter import (
    track_api_response,
    track_openai_call,
    estimate_tokens_and_cost,
    count_tokens_tiktoken,
    extract_tokens_from_response,
    LangChainTokenCallback,
    HybridTokenCounter,
    invalidate_model_price_cache
)

__all__ = [
    "track_api_response",
    "track_openai_call",
    "estimate_tokens_and_cost",
    "count_tokens_tiktoken",
    "extract_tokens_from_response",
    "LangChainTokenCallback",
    "HybridTokenCounter",
    "invalidate_model_price_cache",
]
```

---

## 📋 Passo 4: Publicar Pacote

### 4.1 Commit inicial

```bash
git add .
git commit -m "Initial commit: Automaxia Utils v1.0.0"
git tag v1.0.0
git push origin main --tags
```

---

## 📋 Passo 5: Usar nos Projetos

### 5.1 Instalar em cada projeto

**Projeto 1:**
```bash
cd /caminho/do/projeto1
pip install git+https://github.com/automaxia/automaxia-utils.git
```

**Projeto 2:**
```bash
cd /caminho/do/projeto2
pip install git+https://github.com/automaxia/automaxia-utils.git
```

### 5.2 Atualizar imports nos projetos

**ANTES (código antigo):**
```python
from utils.token_counter import track_api_response
from utils.admin_center_service import get_admin_center_service
```

**DEPOIS (novo pacote):**
```python
from automaxia_utils import track_api_response, get_admin_center_service
```

### 5.3 Remover arquivos antigos dos projetos

```bash
# Em cada projeto, remover:
rm utils/token_counter.py
rm utils/admin_center_service.py
```

---

## 📋 Passo 6: Workflow de Desenvolvimento

### 6.1 Fazer alterações no pacote

```bash
cd automaxia-utils

# Editar arquivos
vim automaxia_utils/token_tracking/counter.py

# Testar localmente em um projeto
cd /caminho/do/projeto1
pip install -e /caminho/para/automaxia-utils
```

### 6.2 Publicar nova versão

```bash
cd automaxia-utils

# Atualizar versão no setup.py e __init__.py
# version="1.1.0"

git add .
git commit -m "feat: Nova funcionalidade X"
git tag v1.1.0
git push origin main --tags
```

### 6.3 Atualizar nos projetos

```bash
# Em cada projeto
pip install --upgrade git+https://github.com/automaxia/automaxia-utils.git
```

---

##