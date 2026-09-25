"""JobRunner atendendo a agenda de produtos derivados (1.19.0).

O Forge (SDD §5.12 do ecossistema) roda a agenda dos produtos que hospeda. O que
fica travado aqui: o job do filho chega ao handler certo, dentro do escopo do
filho (senao o consumo cairia no pai), e dois filhos com o mesmo slug nao se
atropelam.
"""
from automaxia_utils.admin_center import service as svc_mod
from automaxia_utils.admin_center.jobs import JobRunner
from automaxia_utils.admin_center.service import AdminCenterConfig, AdminCenterService

PAI = "11111111-1111-1111-1111-111111111111"
FILHO_A = "aaaaaaaa-0000-4000-8000-00000000000a"
FILHO_B = "bbbbbbbb-0000-4000-8000-00000000000b"


def _job(id_, slug, product_id):
    return {"id": id_, "slug": slug, "name": slug, "cron_expression": None, "timezone": "America/Sao_Paulo",
            "is_enabled": True, "max_instances": 1, "config_version": 1, "status": "active",
            "product_id": product_id}


def _runner(monkeypatch, jobs, filhos=True):
    s = AdminCenterService(AdminCenterConfig(enabled=False, product_id=PAI,
                                             environment_id="22222222-2222-2222-2222-222222222222"))
    s.config.enabled = True
    chamadas = []

    def _req(metodo, endpoint, dados=None, params=None, **kw):
        chamadas.append((metodo, endpoint, params))
        if endpoint == "/agent/job":
            return {"data": jobs}
        if endpoint.endswith("/run"):
            return {"data": {"id": "run-" + endpoint.split("/")[3]}}
        return {"success": True}

    monkeypatch.setattr(s, "_make_request", _req)
    return JobRunner(s, produtos_filhos=filhos), chamadas


def test_lista_pede_os_filhos(monkeypatch):
    r, chamadas = _runner(monkeypatch, [])
    r.reload_jobs()
    assert chamadas[0][2]["incluir_filhos"] == "true"


def test_sem_a_flag_nao_pede(monkeypatch):
    r, chamadas = _runner(monkeypatch, [], filhos=False)
    r.reload_jobs()
    assert "incluir_filhos" not in chamadas[0][2]


def test_filhos_com_o_mesmo_slug_nao_se_atropelam(monkeypatch):
    r, _ = _runner(monkeypatch, [_job("j1", "forge-agenda", FILHO_A), _job("j2", "forge-agenda", FILHO_B),
                                 _job("j0", "limpeza", PAI)])
    r.reload_jobs()
    assert set(r._jobs) == {f"{FILHO_A}:forge-agenda", f"{FILHO_B}:forge-agenda", "limpeza"}


def test_job_do_filho_roda_no_handler_de_derivados_dentro_do_escopo(monkeypatch):
    r, chamadas = _runner(monkeypatch, [_job("j1", "forge-agenda", FILHO_A)])
    vistos = []
    r.register_derivados(lambda cfg: vistos.append((cfg.product_id, cfg.slug, svc_mod._product_scope.get())))
    r.reload_jobs()
    assert r.run_job(f"{FILHO_A}:forge-agenda")
    assert vistos == [(FILHO_A, "forge-agenda", FILHO_A)]
    # o escopo nao vaza para depois do job
    assert svc_mod._product_scope.get() is None
    # run criado e encerrado pelo id do job do filho
    assert ("POST", "/agent/job/j1/run", None) in chamadas
    assert any(e == "/agent/job/run/run-j1/finish" for _, e, _p in chamadas)


def test_job_do_proprio_produto_segue_no_handler_por_slug(monkeypatch):
    r, _ = _runner(monkeypatch, [_job("j0", "limpeza", PAI)])
    rodou = []
    r.register("limpeza", lambda: rodou.append(svc_mod._product_scope.get()))
    r.register_derivados(lambda cfg: rodou.append("errado"))
    r.reload_jobs()
    assert r.run_job("limpeza")
    assert rodou == [None]


def test_filho_sem_handler_de_derivados_nao_roda(monkeypatch):
    r, _ = _runner(monkeypatch, [_job("j1", "forge-agenda", FILHO_A)])
    r.reload_jobs()
    assert not r.run_job(f"{FILHO_A}:forge-agenda")
