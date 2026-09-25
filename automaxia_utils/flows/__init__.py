"""Fluxos de agentes (1.19.0) — SDD do ecossistema §5.11 e §5.12."""
from .runner import (
    ContextoFluxo,
    FalhaDoNo,
    Ferramenta,
    FlowRunner,
    FluxoInvalido,
    IaNaoConfigurada,
    LimiteExcedido,
    PedidoLLM,
    RegistroDeFerramentas,
    RespostaLLM,
    ResultadoFluxo,
    validar_fluxo,
)

__all__ = [
    "ContextoFluxo", "FalhaDoNo", "Ferramenta", "FlowRunner", "FluxoInvalido",
    "IaNaoConfigurada", "LimiteExcedido", "PedidoLLM", "RegistroDeFerramentas",
    "RespostaLLM", "ResultadoFluxo", "validar_fluxo",
]
