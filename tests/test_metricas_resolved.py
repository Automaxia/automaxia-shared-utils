"""Camada semantica (1.18.0): as metricas chegam no ResolvedConnection."""
from automaxia_utils.admin_center.connections import ResolvedConnection

BASE = {"id": "c1", "alias": "a", "engine": "postgresql", "password": "p",
        "expires_at": "2026-01-01T00:00:00+00:00"}


def test_metricas_do_resolve_chegam_no_dto():
    m = {"name": "Receita", "slug": "receita", "synonyms": ["faturamento"],
         "base_table": "vendas.pedidos", "expression": "SUM(valor)",
         "filter_sql": None, "time_column": "data", "format": "moeda", "description": None}
    r = ResolvedConnection.from_dict({**BASE, "metrics": [m]})
    assert r.metrics == [m]


def test_admincenter_antigo_sem_o_campo_vira_lista_vazia():
    assert ResolvedConnection.from_dict(BASE).metrics == []
    assert ResolvedConnection.from_dict({**BASE, "metrics": None}).metrics == []
