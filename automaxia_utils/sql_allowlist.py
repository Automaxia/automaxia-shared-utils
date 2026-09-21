"""Allowlist de tabelas por conexao: o SQL so' pode ler os objetos liberados.

Responsável técnico: Wesley Romualdo da Silva

A conexao do cofre carrega `allowed_tables` (AdminCenter, migration 0053;
entregue no `/database-connection/resolve`). Os satelites (Talk, Vision) chamam
`verificar_sql` ANTES de executar qualquer SQL no banco do cliente e
`filtrar_tabelas` ao listar o schema. E' uma segunda barreira, independente dos
GRANTs da credencial: se um GRANT a mais escapar, a allowlist segura.

Portado do ecossistema-irmao Balance (infrabalance-utils 2.11.1).

Regras
------
- Allowlist vazia/None -> SEM restricao (comportamento anterior).
- Itens: `schema.objeto`, `schema.*` (schema inteiro) ou `objeto` (no schema
  padrao da conexao). BigQuery: `dataset.tabela`; `projeto.dataset.tabela` so'
  casa se o projeto for o da conexao. Comparacao sem diferenciar maiusculas.
- Referencia sem schema no SQL resolve pelo `schema_padrao` (search_path /
  dataset padrao).
- Nomes de CTE nao contam; catalogos de sistema (`information_schema`,
  `pg_catalog`) sao permitidos (so' expoem nomes, e o proprio produto os usa).
- Funcao no FROM so' `generate_series`/`unnest`; qualquer outra e' recusada.
- Nao conseguiu ler o SQL -> RECUSA (fail-closed, so' quando ha allowlist).
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

DIALETOS_SQLGLOT = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mysql": "mysql",
    "mssql": "tsql",
    "oracle": "oracle",
    "databricks": "databricks",
    "bigquery": "bigquery",
}

SCHEMAS_DE_SISTEMA = frozenset({"information_schema", "pg_catalog"})
FUNCOES_PERMITIDAS = frozenset({"generate_series", "unnest"})


class TabelaNaoLiberada(PermissionError):
    """O SQL le objeto fora da allowlist da conexao (ou nao pode ser verificado)."""

    def __init__(self, tabelas: Sequence[str], motivo: Optional[str] = None):
        self.tabelas = list(tabelas)
        self.motivo = motivo
        texto = motivo or (
            "Consulta a objeto nao liberado para esta conexao: " + ", ".join(self.tabelas)
        )
        super().__init__(texto)


def normalizar_allowlist(itens) -> Optional[tuple]:
    """Tupla de itens em minusculas, ou None quando nao ha restricao."""
    if not itens:
        return None
    if isinstance(itens, str):
        itens = [p for p in itens.replace(";", ",").replace("\n", ",").split(",")]
    limpos = []
    for item in itens:
        t = str(item or "").strip().strip('`"[]').lower()
        if t and t not in limpos:
            limpos.append(t)
    return tuple(limpos) or None


def _separar(nome: str) -> tuple:
    partes = [p.strip('`"[] ') for p in nome.split(".") if p.strip()]
    if len(partes) >= 2:
        return partes[-2], partes[-1]
    return None, partes[0] if partes else ""


def liberada(schema: Optional[str], nome: str, allowlist, schema_padrao: Optional[str] = None) -> bool:
    """True se `schema.nome` esta na allowlist (None = sem restricao)."""
    permitidos = normalizar_allowlist(allowlist)
    if permitidos is None:
        return True
    s = (schema or schema_padrao or "").strip().lower()
    n = (nome or "").strip().lower()
    if s in SCHEMAS_DE_SISTEMA:
        return True
    padrao = (schema_padrao or "").strip().lower()
    for item in permitidos:
        i_schema, i_nome = _separar(item)
        i_schema = (i_schema or padrao).lower()
        if i_nome == "*" and i_schema == s:
            return True
        if i_nome == n and i_schema == s:
            return True
    return False


def filtrar_tabelas(itens: Iterable, allowlist, schema_padrao: Optional[str] = None,
                    chave_schema: str = "schema", chave_nome: str = "tabela") -> list:
    """Filtra uma listagem de objetos. Aceita dicts ou tuplas (schema, nome)."""
    saida = []
    for item in itens:
        if isinstance(item, dict):
            s, n = item.get(chave_schema), item.get(chave_nome)
        else:
            s, n = item[0], item[1]
        if liberada(s, n, allowlist, schema_padrao):
            saida.append(item)
    return saida


def tabelas_referenciadas(sql: str, dialeto: str = "postgresql",
                          schema_padrao: Optional[str] = None,
                          catalogo_padrao: Optional[str] = None) -> set:
    """Objetos que o SQL le, como `schema.nome` (minusculas).

    Levanta TabelaNaoLiberada se o SQL nao puder ser lido ou usar funcao no FROM
    fora de FUNCOES_PERMITIDAS."""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError as exc:  # pragma: no cover - dependencia do extra [sql]
        raise TabelaNaoLiberada([], "Verificador de SQL indisponivel (sqlglot ausente).") from exc

    leitor = DIALETOS_SQLGLOT.get((dialeto or "postgresql").lower(), "postgres")
    try:
        arvores = [a for a in sqlglot.parse(sql, read=leitor) if a is not None]
    except Exception as exc:
        raise TabelaNaoLiberada([], f"Nao foi possivel verificar o SQL ({type(exc).__name__}).") from exc
    if not arvores:
        raise TabelaNaoLiberada([], "SQL vazio.")

    padrao = (schema_padrao or "").strip().lower() or None
    catalogo = (catalogo_padrao or "").strip().lower() or None
    refs = set()
    for arvore in arvores:
        ctes = {c.alias_or_name.lower() for c in arvore.find_all(exp.CTE)}
        for t in arvore.find_all(exp.Table):
            if not isinstance(t.this, exp.Identifier):
                funcao = (t.this.sql_name() if hasattr(t.this, "sql_name") else type(t.this).__name__).lower()
                if not any(f in funcao for f in ("generate", "unnest", "series")):
                    raise TabelaNaoLiberada([funcao], f"Funcao nao permitida no FROM: {funcao}.")
                continue
            nome = t.name.lower()
            schema = (t.db or "").lower() or None
            cat = (t.catalog or "").lower() or None
            if schema is None and nome in ctes:
                continue
            if nome in FUNCOES_PERMITIDAS and schema is None:
                continue
            if schema is None and nome.startswith("pg_") and leitor == "postgres":
                # Catalogo do Postgres sem schema (pg_class, pg_namespace...): o
                # search_path sempre resolve para pg_catalog. O satelite le o schema
                # assim; tratar como tabela do schema padrao recusava tudo (bug do Balance 2.11.0).
                continue
            schema = schema or padrao or ""
            if cat and catalogo and cat != catalogo:
                refs.add(f"{cat}.{schema}.{nome}")
            elif cat and not catalogo and schema not in SCHEMAS_DE_SISTEMA:
                refs.add(f"{cat}.{schema}.{nome}")
            else:
                refs.add(f"{schema}.{nome}" if schema else nome)
    return refs


def verificar_sql(sql: str, allowlist, dialeto: str = "postgresql",
                  schema_padrao: Optional[str] = None,
                  catalogo_padrao: Optional[str] = None) -> None:
    """Levanta TabelaNaoLiberada se o SQL ler algo fora da allowlist.

    Sem allowlist, nao faz nada (nem tenta ler o SQL)."""
    if normalizar_allowlist(allowlist) is None:
        return
    fora = []
    for ref in sorted(tabelas_referenciadas(sql, dialeto, schema_padrao, catalogo_padrao)):
        partes = ref.split(".")
        if len(partes) == 3:
            fora.append(ref)  # outro projeto/catalogo: nunca liberado por item de 2 partes
            continue
        schema, nome = (partes[0], partes[1]) if len(partes) == 2 else (None, partes[0])
        if not liberada(schema, nome, allowlist, schema_padrao):
            fora.append(ref)
    if fora:
        raise TabelaNaoLiberada(fora)
