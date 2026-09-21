"""Conexoes engine='bigquery' no ResolvedConnection / ConnectionResolver.

Responsável técnico: Wesley Romualdo da Silva

Nao depende de google-cloud-bigquery instalado: cobre o contrato do DTO
(DSN, credencial) e as recusas explicitas dos caminhos que so servem Postgres.
"""
import json

import pytest

from automaxia_utils.admin_center.connections import ConnectionResolver, ResolvedConnection

SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "meu-projeto-123",
    "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
    "client_email": "leitor@meu-projeto-123.iam.gserviceaccount.com",
}


def _payload(**over):
    base = {
        "id": "00000000-0000-0000-0000-000000000001",
        "alias": "bq-vendas",
        "engine": "bigquery",
        "database_name": "meu-projeto-123",
        "schema_name": "vendas",
        "username": SERVICE_ACCOUNT["client_email"],
        "password": json.dumps(SERVICE_ACCOUNT),
        "version": 1,
        "expires_at": "2099-01-01T00:00:00Z",
    }
    base.update(over)
    return base


def test_dsn_bigquery_sem_credencial_na_url():
    resolved = ResolvedConnection.from_dict(_payload())
    assert resolved.is_bigquery
    assert resolved.dsn() == "bigquery://meu-projeto-123/vendas"
    assert "private_key" not in resolved.dsn()


def test_dsn_bigquery_sem_dataset_aponta_so_para_o_projeto():
    resolved = ResolvedConnection.from_dict(_payload(schema_name=None))
    assert resolved.dsn() == "bigquery://meu-projeto-123"


def test_credentials_info_devolve_o_json():
    resolved = ResolvedConnection.from_dict(_payload())
    assert resolved.bigquery_credentials_info()["client_email"] == SERVICE_ACCOUNT["client_email"]


def test_credentials_info_recusa_json_que_nao_e_service_account():
    resolved = ResolvedConnection.from_dict(_payload(password='{"type": "authorized_user"}'))
    with pytest.raises(ValueError):
        resolved.bigquery_credentials_info()


def test_credentials_info_recusa_engine_sql():
    resolved = ResolvedConnection.from_dict(
        _payload(engine="postgresql", host="h", port=5432, password="x")
    )
    with pytest.raises(ValueError):
        resolved.bigquery_credentials_info()


def test_get_psycopg2_recusa_conexao_bigquery(monkeypatch):
    resolver = ConnectionResolver(admin_service=None)
    monkeypatch.setattr(
        resolver, "resolve", lambda **_: ResolvedConnection.from_dict(_payload())
    )
    with pytest.raises(RuntimeError, match="BigQuery"):
        resolver.get_psycopg2("bq-vendas")
