"""Aviso de escritorio al final de radar.run y registro de corridas."""
import logging
import sys
import types
from datetime import timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from radar import notify, run
from radar.db import init_db, make_engine
from radar.ingest import upsert_jobs
from radar.models import Run, utcnow
from radar.tracking import count_new, finish_run, last_run_start, start_run

from tests.test_search import job


@pytest.fixture
def Session(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'n.db').as_posix()}")
    init_db(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def _close_logs():
    yield
    logger = logging.getLogger("radar")
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def test_format_message():
    assert notify.format_message(5, 2)[1].startswith("5 ofertas nuevas, 2 prometedoras")
    assert notify.format_message(1, 1)[1].startswith("1 oferta nueva, 1 prometedora")
    assert notify.format_message(0, 0)[1] == "0 ofertas nuevas, 0 prometedoras"


def test_notify_desktop_uses_click_url(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "win11toast",
                        types.SimpleNamespace(notify=lambda *a, **k: calls.append((a, k))))
    assert notify.notify_desktop("T", "B") is True
    assert calls == [(("T", "B"), {"on_click": "http://127.0.0.1:8000/?nuevas=1"})]


def test_notify_desktop_without_library_only_logs(monkeypatch):
    monkeypatch.setitem(sys.modules, "win11toast", None)  # simula ImportError
    logs = []
    assert notify.notify_desktop("T", "3 ofertas nuevas", log=logs.append) is False
    assert "no disponible" in logs[0] and "3 ofertas nuevas" in logs[0]


def test_runs_and_new_counts(Session):
    with Session() as s:
        assert last_run_start(s) is None
        r = start_run(s)
        upsert_jobs(s, [job(id="a:1", source="a", title="Desarrollador Junior Python", company="A",
                            description="Python, SQL, Git"),
                        job(id="a:2", source="a", title="Senior Staff Architect", company="B")])
        assert count_new(s, r.started_at) == 2
        assert count_new(s, r.started_at, promising_only=True) == 1
        assert count_new(s, utcnow() + timedelta(minutes=1)) == 0
        assert last_run_start(s) is None  # aún sin terminar
        finish_run(s, r, 2, 1)
        assert last_run_start(s) is not None


def _patch_run(monkeypatch, Session, notified):
    monkeypatch.setattr(run, "wait_for_network", lambda *a, **k: True)
    monkeypatch.setattr(run, "SessionLocal", Session)
    monkeypatch.setattr(run, "init_db", lambda: None)
    monkeypatch.setattr(run, "notify_desktop", lambda title, body, log=None: notified.append(body))

    def fake_ingest(sources, log, errors):
        with Session() as s:
            new, _ = upsert_jobs(s, [job(id="a:1", source="a", title="Desarrollador Junior Python",
                                         company="A", description="Python, SQL, Git")])
        errors["caida"] = "ConnectError: sin red"
        return {"a": (new, 0), "caida": (0, 0)}

    monkeypatch.setattr(run, "ingest", fake_ingest)


def test_run_notifies_and_records_run(Session, monkeypatch, tmp_path):
    notified = []
    _patch_run(monkeypatch, Session, notified)
    assert run.main(["--no-score", "--log-file", str(tmp_path / "r.log")]) == 0
    assert notified == ["1 oferta nueva, 1 prometedora. Clic para revisarlas en el panel."]
    with Session() as s:
        r = s.query(Run).one()
        assert (r.new_jobs, r.promising, r.finished_at is not None) == (1, 1, True)
        assert "caida" in r.errors


def test_run_no_notify_flag(Session, monkeypatch, tmp_path):
    notified = []
    _patch_run(monkeypatch, Session, notified)
    assert run.main(["--no-score", "--no-notify", "--log-file", str(tmp_path / "r.log")]) == 0
    assert notified == [] and "1 oferta nueva" in (tmp_path / "r.log").read_text(encoding="utf-8")


def test_run_score_setup_error_is_logged_not_fatal(Session, monkeypatch, tmp_path):
    _patch_run(monkeypatch, Session, [])

    def no_cv(*a, **k):
        raise run.ScoreSetupError("No encuentro el CV")

    monkeypatch.setattr(run, "score_cli", no_cv)
    assert run.main(["--no-notify", "--log-file", str(tmp_path / "r.log")]) == 0
    assert "No encuentro el CV" in (tmp_path / "r.log").read_text(encoding="utf-8")
