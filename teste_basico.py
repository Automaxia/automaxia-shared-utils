# teste_basico.py
from automaxia_utils import track_api_response
from openai import OpenAI
import os

# Configurar cliente OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Fazer chamada
response = client.chat.completions.create(
    model="gpt-3.5-turbo",
    messages=[{"role": "user", "content": "Olá!"}]
)

# Rastrear (tokens REAIS, não estimativa)
tracking = track_api_response(
    response=response,
    model="gpt-3.5-turbo",
    endpoint="/test"
)

print(f"Tokens: {tracking['total_tokens']}")
print(f"Custo: ${tracking['cost_usd']:.6f}")
print(f"Fonte: {tracking['source']}")  # Deve ser "api_response"