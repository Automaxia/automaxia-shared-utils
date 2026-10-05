"""`gatilho_cron` dispara nos mesmos horários que o croniter (o motor do AdminCenter).

LIB-43: o `CronTrigger.from_crontab` do APScheduler 3.x lê o dia da semana com 0 = segunda
(`1-5` rodava de terça a sábado). LIB-44: `0-2` virava `sun-tue`, que o APScheduler recusa.
"""
import datetime as dt

import pytest

croniter = pytest.importorskip("croniter").croniter
pytest.importorskip("apscheduler")
pytz = pytest.importorskip("pytz")

from automaxia_utils.admin_center.jobs import gatilho_cron  # noqa: E402

FUSO = "America/Sao_Paulo"

CRONS = [
    "0 8 * * 1-5", "30 7 * * 0", "0 6 * * 6,0", "0 8 * * 7", "0 10 * * *", "0 8 * * mon-fri",
    "0 8 * * 1-5/2", "0 8 * * */2", "0 8 * * 5-6", "0 12 1 * *", "0 8 * * 0-2", "0 8 * * 5-7",
    "0 8 * * 0-6", "0 8 * * 1,3", "0 8 * * 0-3/2", "0 8 * * 3/2", "0 9 * * 1-3,5", "0 8 * * 0-7",
    "0 8 * * 1/3",
]


@pytest.mark.parametrize("expressao", CRONS)
def test_mesmos_disparos_que_o_croniter(expressao):
    gatilho = gatilho_cron(expressao, FUSO)
    instante = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)   # sábado, 09:00 em Brasília
    nossos = []
    for _ in range(10):
        instante = gatilho.get_next_fire_time(None, instante + dt.timedelta(seconds=1))
        nossos.append(instante.astimezone(dt.timezone.utc))
    ref = croniter(expressao, pytz.timezone(FUSO).localize(dt.datetime(2026, 10, 3, 9, 0, 1)))
    esperados = [ref.get_next(dt.datetime).astimezone(dt.timezone.utc) for _ in range(10)]
    assert nossos == esperados


def test_dias_uteis_nao_rodam_no_sabado():
    gatilho = gatilho_cron("0 8 * * 1-5", FUSO)
    sabado = dt.datetime(2026, 10, 3, 10, 0, tzinfo=dt.timezone.utc)
    proximo = gatilho.get_next_fire_time(None, sabado)
    assert proximo.strftime("%a") == "Mon"
