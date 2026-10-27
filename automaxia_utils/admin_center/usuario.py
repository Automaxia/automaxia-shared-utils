"""Usuario da requisicao atual — a quem o consumo de IA pertence (1.22.0, LIB-46 / ST-107).

O `token_usage` era atribuido ao DONO do produto: Talk, Vision, Viewer e
Cartografo sao da organizacao da plataforma, entao o consumo de um cliente
usando o Talk caia na conta da Automaxia, e nenhum teto por cliente tinha o
que medir. Quase nenhum satelite passava `user_id` ao `track_token_usage`.

Agora o `AdminCenterAuth.validate_token` marca aqui quem e o usuario da
requisicao (e a organizacao dele), e o `track_token_usage` usa isso quando o
chamador nao passou `user_id`. O AdminCenter deduz a organizacao pelo usuario
(ou, sem usuario, pela do produto — o derivado do Forge ja e do cliente).

ContextVar, nao global: cada requisicao (task asyncio) tem o proprio valor, e
`asyncio.to_thread`/`run_in_threadpool` copiam o contexto. Thread criada a mao
(`threading.Thread`) NAO herda — ali o consumo cai na organizacao do produto.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional, Tuple

_usuario: ContextVar[Optional[Tuple[Optional[str], Optional[str]]]] = ContextVar(
    "automaxia_usuario_da_requisicao", default=None
)


def definir_usuario_da_requisicao(user_id: Optional[str], organization_id: Optional[str] = None) -> None:
    """Marca o usuario (e a organizacao) donos da requisicao atual."""
    if user_id or organization_id:
        _usuario.set((str(user_id) if user_id else None,
                      str(organization_id) if organization_id else None))


def usuario_da_requisicao() -> Tuple[Optional[str], Optional[str]]:
    """(user_id, organization_id) da requisicao atual, ou (None, None)."""
    valor = _usuario.get()
    return valor if valor else (None, None)


__all__ = ["definir_usuario_da_requisicao", "usuario_da_requisicao"]
