"""Allowlist de tabelas por conexao (automaxia_utils.sql_allowlist).

Responsável técnico: Wesley Romualdo da Silva
"""
import pytest

from automaxia_utils.sql_allowlist import (
    TabelaNaoLiberada,
    filtrar_tabelas,
    liberada,
    normalizar_allowlist,
    tabelas_referenciadas,
    verificar_sql,
)

LISTA = ["vendas.vw_resumo"]


def test_sem_allowlist_nao_restringe_nem_le_o_sql():
    verificar_sql("isto nao e SQL ;;;", None)
    verificar_sql("SELECT * FROM qualquer.coisa", [])
    assert normalizar_allowlist("  ") is None


def test_normaliza_texto_e_lista():
    assert normalizar_allowlist("Vendas.VW_X, s.*\nt") == ("vendas.vw_x", "s.*", "t")
    assert normalizar_allowlist(["S.T", "s.t", " "]) == ("s.t",)


def test_view_liberada_passa():
    verificar_sql("SELECT periodo FROM vendas.vw_resumo", LISTA)


def test_sem_schema_usa_o_padrao():
    verificar_sql("SELECT * FROM vw_resumo", LISTA, schema_padrao="vendas")
    with pytest.raises(TabelaNaoLiberada):
        verificar_sql("SELECT * FROM vw_resumo", LISTA, schema_padrao="public")


def test_tabela_fora_da_lista_e_recusada():
    with pytest.raises(TabelaNaoLiberada) as exc:
        verificar_sql("SELECT centro_custo, margem FROM vendas.tb_custos_internos", LISTA)
    assert exc.value.tabelas == ["vendas.tb_custos_internos"]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM vendas.vw_resumo v JOIN vendas.tb_execucoes r ON true",
    "SELECT * FROM vendas.vw_resumo WHERE x IN (SELECT y FROM vendas.tb_precos)",
    "WITH a AS (SELECT * FROM vendas.tb_custos_internos) SELECT * FROM a",
    "SELECT * FROM vendas.vw_resumo UNION ALL SELECT * FROM outro.t",
    "SELECT (SELECT max(1) FROM vendas.tb_execucoes) FROM vendas.vw_resumo",
])
def test_subconsulta_join_cte_e_union_nao_escapam(sql):
    with pytest.raises(TabelaNaoLiberada):
        verificar_sql(sql, LISTA)


def test_cte_com_nome_proprio_nao_conta():
    verificar_sql("WITH ano AS (SELECT * FROM vendas.vw_resumo) SELECT * FROM ano", LISTA)


def test_catalogo_de_sistema_e_generate_series():
    verificar_sql("SELECT column_name FROM information_schema.columns", LISTA)
    verificar_sql("SELECT * FROM generate_series(1, 3) g", LISTA)


def test_funcao_estranha_no_from_e_recusada():
    with pytest.raises(TabelaNaoLiberada):
        verificar_sql("SELECT * FROM dblink('x', 'select 1') AS t(a int)", LISTA)


def test_sql_ilegivel_e_recusado_com_allowlist():
    with pytest.raises(TabelaNaoLiberada):
        verificar_sql("SELEC * FORM ((", LISTA)


def test_curinga_de_schema():
    verificar_sql("SELECT * FROM comercial.qualquer JOIN comercial.outra ON true", ["comercial.*"])
    with pytest.raises(TabelaNaoLiberada):
        verificar_sql("SELECT * FROM public.t", ["comercial.*"])


def test_bigquery_dataset_e_projeto():
    lista = ["ds01.pedidos"]
    verificar_sql("SELECT COUNT(*) FROM `ds01.pedidos`", lista, dialeto="bigquery")
    verificar_sql("SELECT 1 FROM `projeto-exemplo.ds01.pedidos`", lista, dialeto="bigquery",
                  catalogo_padrao="projeto-exemplo")
    verificar_sql("SELECT 1 FROM pedidos", lista, dialeto="bigquery", schema_padrao="ds01")
    with pytest.raises(TabelaNaoLiberada):
        verificar_sql("SELECT 1 FROM `outro-projeto.ds01.pedidos`", lista, dialeto="bigquery",
                      catalogo_padrao="projeto-exemplo")
    with pytest.raises(TabelaNaoLiberada):
        verificar_sql("SELECT 1 FROM `ds01.clientes`", lista, dialeto="bigquery")


def test_sql_server_e_maiusculas():
    verificar_sql("SELECT TOP (5) * FROM DBO.Vendas", ["dbo.vendas"], dialeto="mssql")


def test_referencias_em_minusculas():
    assert tabelas_referenciadas('SELECT * FROM "S"."T" JOIN u ON true', schema_padrao="p") == {"s.t", "p.u"}


def test_filtrar_listagem():
    itens = [{"schema": "vendas", "tabela": "vw_resumo"},
             {"schema": "vendas", "tabela": "tb_custos_internos"}]
    assert filtrar_tabelas(itens, LISTA) == itens[:1]
    assert filtrar_tabelas([("vendas", "tb_x")], LISTA) == []
    assert filtrar_tabelas(itens, None) == itens
    assert liberada("VENDAS", "VW_RESUMO", LISTA)


def test_catalogo_do_postgres_sem_schema_passa():
    """O satelite le o schema com pg_class/pg_namespace sem qualificar (bug do Balance 2.11.0)."""
    sql = ("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
           "LEFT JOIN pg_description d ON d.objoid = c.oid WHERE n.nspname = 'vendas'")
    verificar_sql(sql, LISTA, schema_padrao="vendas")
    with pytest.raises(TabelaNaoLiberada):
        verificar_sql("SELECT * FROM pg_class c JOIN tb_custos_internos t ON true", LISTA, schema_padrao="vendas")
    with pytest.raises(TabelaNaoLiberada):
        # fora do Postgres, pg_* nao e' catalogo
        verificar_sql("SELECT * FROM pg_segredo", ["ds.t"], dialeto="bigquery", schema_padrao="ds")
