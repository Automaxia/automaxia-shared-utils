"""Teto mensal de IA da organização (1.22.0, LIB-47 / ST-108).

O AdminCenter mede o gasto de IA do mês por organização (`token_usage.
organization_id`) contra `billing_settings.ai_monthly_limit` e responde em
`GET /billing/consumo-ia/situacao`. Aqui fica o lado cliente:

- `AdminCenterService.situacao_consumo_ia(...)`: a consulta, com cache de 60 s por
  (organização ou produto) — o gasto muda devagar, e o LLM não pode esperar uma
  ida ao AdminCenter a cada chamada.
- `AdminCenterService.verificar_teto_ia(...)`: levanta `TetoDeIaAtingido` quando o
  teto estourou. É o que o `TetoDeIaMiddleware` (rotas de IA) e o `FlowRunner`
  (agenda, webhook, WhatsApp) chamam antes do LLM.

FAIL-OPEN: AdminCenter fora do ar, rota antiga (404) ou erro de rede → a IA segue.
O teto protege o bolso; não pode derrubar o produto.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

CACHE_S = 60.0

_cache: Dict[Tuple[str, str], Tuple[float, Optional[Dict[str, Any]]]] = {}
_trava = threading.Lock()


class TetoDeIaAtingido(RuntimeError):
    """O teto mensal de IA da organização foi atingido."""

    def __init__(self, mensagem: str, situacao: Optional[Dict[str, Any]] = None):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.situacao = situacao or {}


def do_cache(chave: Tuple[str, str]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    with _trava:
        item = _cache.get(chave)
    if item and time.monotonic() - item[0] < CACHE_S:
        return True, item[1]
    return False, None


def guardar(chave: Tuple[str, str], valor: Optional[Dict[str, Any]]) -> None:
    with _trava:
        if len(_cache) > 10_000:
            _cache.clear()
        _cache[chave] = (time.monotonic(), valor)


def limpar_cache() -> None:
    with _trava:
        _cache.clear()


__all__ = ["TetoDeIaAtingido", "limpar_cache"]
