#!/usr/bin/env python3
"""
Script de teste para validar instalação do automaxia-utils no Python 3.13.2
Execute: python teste_instalacao_python313.py
"""
import sys
import platform

print("="*70)
print("🐍 TESTE DE INSTALAÇÃO - AUTOMAXIA UTILS")
print("="*70)

# 1. Verificar versão do Python
print(f"\n1️⃣ Versão do Python:")
print(f"   {platform.python_version()}")
assert sys.version_info >= (3, 8), "❌ Python 3.8+ necessário"
print("   ✅ Versão compatível")

# 2. Testar importação principal
print(f"\n2️⃣ Importando automaxia_utils...")
try:
    import automaxia_utils
    print(f"   ✅ Pacote importado - versão {automaxia_utils.__version__}")
except ImportError as e:
    print(f"   ❌ Erro ao importar: {e}")
    print("   💡 Execute: pip install git+https://github.com/automaxia/automaxia-utils.git")
    sys.exit(1)

# 3. Testar funções principais
print(f"\n3️⃣ Testando funções de token tracking...")
try:
    from automaxia_utils import (
        track_api_response,
        estimate_tokens_and_cost,
        count_tokens_tiktoken
    )
    print("   ✅ Funções de token tracking OK")
except ImportError as e:
    print(f"   ❌ Erro: {e}")
    sys.exit(1)

# 4. Testar Admin Center
print(f"\n4️⃣ Testando Admin Center Service...")
try:
    from automaxia_utils import get_admin_center_service, AdminCenterConfig
    admin = get_admin_center_service()
    print(f"   ✅ Admin Center Service OK")
    print(f"   📊 Habilitado: {admin.config.enabled}")
    print(f"   📊 Válido: {admin.config.is_valid()}")
except ImportError as e:
    print(f"   ❌ Erro: {e}")
    sys.exit(1)

# 5. Testar contagem de tokens
print(f"\n5️⃣ Testando contagem de tokens (tiktoken)...")
try:
    texto_teste = "Olá, mundo! Este é um teste."
    tokens = count_tokens_tiktoken(texto_teste, "gpt-3.5-turbo")
    print(f"   ✅ Tokens contados: {tokens}")
    assert tokens > 0, "Contagem deve ser > 0"
except Exception as e:
    print(f"   ❌ Erro: {e}")
    sys.exit(1)

# 6. Testar estimativa de custos
print(f"\n6️⃣ Testando estimativa de custos...")
try:
    estimativa = estimate_tokens_and_cost(
        prompt="Hello, world!",
        model="gpt-3.5-turbo",
        estimated_response_length=50
    )
    print(f"   ✅ Estimativa calculada:")
    print(f"      Tokens: {estimativa['estimated_total_tokens']}")
    print(f"      Custo USD: ${estimativa['cost_usd']:.6f}")
    print(f"      Custo BRL: R${estimativa['cost_brl']:.4f}")
    assert 'cost_usd' in estimativa
    assert 'cost_brl' in estimativa
except Exception as e:
    print(f"   ❌ Erro: {e}")
    sys.exit(1)

# 7. Testar extração de tokens (mock)
print(f"\n7️⃣ Testando extração de tokens de resposta...")
try:
    from automaxia_utils import extract_tokens_from_response
    
    # Simular resposta OpenAI
    class MockUsage:
        prompt_tokens = 10
        completion_tokens = 20
        total_tokens = 30
    
    class MockResponse:
        usage = MockUsage()
    
    tokens = extract_tokens_from_response(MockResponse())
    print(f"   ✅ Extração OK:")
    print(f"      Prompt: {tokens['prompt_tokens']}")
    print(f"      Completion: {tokens['completion_tokens']}")
    print(f"      Total: {tokens['total_tokens']}")
    assert tokens['total_tokens'] == 30
except Exception as e:
    print(f"   ❌ Erro: {e}")
    sys.exit(1)

# 8. Verificar dependências
print(f"\n8️⃣ Verificando dependências...")
dependencias = {
    "requests": "requests",
    "decouple": "python-decouple",
    "tiktoken": "tiktoken"
}

for modulo, nome_pacote in dependencias.items():
    try:
        __import__(modulo)
        print(f"   ✅ {nome_pacote}")
    except ImportError:
        print(f"   ❌ {nome_pacote} não instalado")
        print(f"      pip install {nome_pacote}")

# 9. Verificar LangChain (opcional)
print(f"\n9️⃣ Verificando LangChain (opcional)...")
try:
    import langchain
    print(f"   ✅ LangChain disponível")
    from automaxia_utils import LangChainTokenCallback
    print(f"   ✅ LangChainTokenCallback OK")
except ImportError:
    print(f"   ⚠️  LangChain não instalado (opcional)")
    print(f"      Para instalar: pip install automaxia-utils[langchain]")

# Resultado final
print("\n" + "="*70)
print("✅ TODOS OS TESTES PASSARAM COM SUCESSO!")
print("="*70)
print("\n💡 Próximos passos:")
print("   1. Configure o .env com suas credenciais")
print("   2. Importe: from automaxia_utils import track_api_response")
print("   3. Use em seus projetos!")
print("\n📚 Documentação: README.md")
print("="*70)