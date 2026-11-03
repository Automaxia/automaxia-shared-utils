"""Um run de job = uma linha em process_execution_logs (1.23.0).

Ate' a 1.22, o `log_process` do PROPRIO job dentro do handler (o `track()` da
folha) gravava linhas novas: um `started` que nunca fechava e um `completed`
duplicado — na lista de execucoes do job e somados no faturamento por minuto.
O que fica travado aqui: esses logs nao saem da lib, a saida deles vai no
/finish do run real, e o resto (sub-etapa, fora de job) segue como antes —
agora com horario em UTC com fuso.
"""
from datetime import datetime

from automaxia_utils.admin_center.jobs import JobRunner
from automaxia_utils.admin_center.service import AdminCenterConfig, AdminCenterService

PROD = "11111111-1111-1111-1111-111111111111"


def _job(slug):
    return {"id": "j1", "slug": slug, "name": slug, "cron_expression": None, "timezone": "America/Sao_Paulo",
            "is_enabled": True, "max_instances": 1, "config_version": 1, "status": "active",
            "product_id": PROD}


def _montar(monkeypatch, slug="folha_pagamento.rodada"):
    s = AdminCenterService(AdminCenterConfig(enabled=False, product_id=PROD,
                                             environment_id="22222222-2222-2222-2222-222222222222"))
    s.config.enabled = True
    chamadas, enfileirados = [], []

    def _req(metodo, endpoint, dados=None, params=None, **kw):
        chamadas.append((endpoint, dados))
        if endpoint == "/agent/job":
            return {"data": [_job(slug)]}
        if endpoint.endswith("/run"):
            return {"data": {"id": "run-1"}}
        return {"success": True}

    monkeypatch.setattr(s, "_make_request", _req)
    monkeypatch.setattr(s, "_enqueue_safely", lambda tipo, p: enfileirados.append((tipo, p)) or True)
    r = JobRunner(s)
    return s, r, chamadas, enfileirados


def _finish(chamadas):
    return [d for e, d in chamadas if e.endswith("/finish")]


def test_log_do_proprio_job_nao_vira_linha_e_vai_no_finish(monkeypatch):
    s, r, chamadas, enfileirados = _montar(monkeypatch)

    def handler():
        s.log_process("folha_pagamento.rodada", "started", input_data={"arquivo": "x.xlsx"})
        s.log_process("folha_pagamento.rodada", "completed", duration_ms=1000,
                      output_data={"enviados": 217, "falhas": 0})

    r.register("folha_pagamento.rodada", handler)
    r.reload_jobs()
    assert r.run_job("folha_pagamento.rodada")
    assert enfileirados == []
    assert _finish(chamadas)[0]["output_data"] == {"enviados": 217, "falhas": 0}
    assert _finish(chamadas)[0]["status"] == "completed"


def test_falha_do_handler_leva_a_saida_parcial(monkeypatch):
    s, r, chamadas, enfileirados = _montar(monkeypatch)

    def handler():
        s.log_process("folha_pagamento.rodada", "failed", output_data={"enviados": 3})
        raise RuntimeError("provider caiu")

    r.register("folha_pagamento.rodada", handler)
    r.reload_jobs()
    assert not r.run_job("folha_pagamento.rodada")
    fim = _finish(chamadas)[0]
    assert (fim["status"], fim["error_message"], fim["output_data"]) == ("failed", "provider caiu", {"enviados": 3})
    assert enfileirados == []


def test_sub_etapa_com_outro_nome_continua_virando_linha(monkeypatch):
    s, r, _, enfileirados = _montar(monkeypatch)
    r.register("folha_pagamento.rodada",
               lambda: s.log_process("folha_pagamento.rodada.envio", "completed", duration_ms=10))
    r.reload_jobs()
    assert r.run_job("folha_pagamento.rodada")
    assert [p["process_name"] for _, p in enfileirados] == ["folha_pagamento.rodada.envio"]
    assert enfileirados[0][1]["job_id"] == "j1"


def test_fora_de_job_grava_como_antes(monkeypatch):
    s, _, _, enfileirados = _montar(monkeypatch)
    s.log_process("folha_pagamento.rodada", "started")
    assert len(enfileirados) == 1


def test_horarios_saem_em_utc_com_fuso_e_inicio_reconstruido(monkeypatch):
    s, _, _, enfileirados = _montar(monkeypatch)
    s.log_process("processar_pergunta", "completed", duration_ms=60_000)
    p = enfileirados[0][1]
    ini, fim = datetime.fromisoformat(p["started_at"]), datetime.fromisoformat(p["finished_at"])
    assert fim.utcoffset().total_seconds() == 0
    assert (fim - ini).total_seconds() == 60


def test_contexto_nao_vaza_para_depois_do_run(monkeypatch):
    s, r, _, enfileirados = _montar(monkeypatch)
    r.register("folha_pagamento.rodada", lambda: None)
    r.reload_jobs()
    r.run_job("folha_pagamento.rodada")
    s.log_process("folha_pagamento.rodada", "completed", duration_ms=5)
    assert len(enfileirados) == 1
