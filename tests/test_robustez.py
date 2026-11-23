"""Ejecución desatendida: rutas absolutas, SQLite WAL, fuentes aisladas, logs y red."""
import logging
from contextlib import nullcontext
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from radar import config, ingest, run
from radar.db import init_db, make_engine
from radar.logsetup import setup_logging
from radar.models import Job
from radar.netcheck import wait_for_network

from tests.test_search import job


def test_db_url_is_absolute_to_project_root():
    root = config.ROOT.as_posix()
    assert config.resolve_db_url("sqlite:///radar.db") == f"sqlite:///{root}/radar.db"
    assert config.resolve_db_url("sqlite:///data/x.db") == f"sqlite:///{root}/data/x.db"
    assert config.resolve_db_url("sqlite:///C:/datos/radar.db") == "sqlite:///C:/datos/radar.db"
    assert config.resolve_db_url("sqlite://") == "sqlite://"
    assert config.resolve_db_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert Path(config.CV_PATH).is_absolute() and config.LOG_FILE.is_absolute()


def test_sqlite_uses_wal_and_busy_timeout(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    with engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert c.execute(text("PRAGMA busy_timeout")).scalar() == 15000
    engine.dispose()


@pytest.fixture
def Session(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'r.db').as_posix()}")
    init_db(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def test_ingest_isolates_failing_source(Session, monkeypatch):
    def bad():
        yield job(id="mala:1", source="mala", title="Parcial", company="M")
        raise httpx.ConnectError("sin conexión")

    def streams(sources, client, log):
        yield "mala", bad()
        yield "buena", iter([job(id="buena:1", source="buena", title="QA Junior", company="B")])

    monkeypatch.setattr(ingest, "source_streams", streams)
    logs, errors = [], {}
    results = ingest.run(["mala", "buena"], log=logs.append, session_factory=Session,
                         client_factory=nullcontext, errors=errors)
    # lo descargado antes del error se guarda; la otra fuente sigue
    assert results == {"mala": (1, 0), "buena": (1, 0)}
    assert "ConnectError" in errors["mala"] and "buena" not in errors
    assert any("error" in m for m in logs)
    with Session() as s:
        assert s.get(Job, "buena:1") and s.get(Job, "mala:1")


def test_ingest_db_error_rolls_back_only_that_source(Session, monkeypatch):
    def streams(sources, client, log):
        yield "rota", iter([{"id": "rota:1", "url": "https://x.co/1"}])  # le faltan campos: falla al guardar
        yield "buena", iter([job(id="buena:2", source="buena", title="Soporte TI", company="B")])

    monkeypatch.setattr(ingest, "source_streams", streams)
    errors = {}
    results = ingest.run(["rota", "buena"], log=lambda m: None, session_factory=Session,
                         client_factory=nullcontext, errors=errors)
    assert results == {"rota": (0, 0), "buena": (1, 0)} and "rota" in errors


def test_ingest_downloads_sources_in_parallel_but_writes_in_order(Session, monkeypatch):
    import threading

    b_started = threading.Event()

    def lenta():
        # si las fuentes fueran una tras otra, "rapida" no arrancaría hasta que esta termine
        assert b_started.wait(5), "las fuentes se descargan una tras otra"
        yield job(id="lenta:1", source="lenta", title="QA Junior", company="Acme")

    def rapida():
        b_started.set()
        yield job(id="rapida:1", source="rapida", title="QA Junior", company="Acme S.A.S.")

    monkeypatch.setattr(ingest, "source_streams",
                        lambda sources, client, log: iter([("lenta", lenta()), ("rapida", rapida())]))
    errors = {}
    results = ingest.run(["lenta", "rapida"], log=lambda m: None, session_factory=Session,
                         client_factory=nullcontext, errors=errors)
    assert errors == {}
    # aunque "rapida" termina primero, se escribe en el orden pedido: la duplicada es la segunda
    assert results == {"lenta": (1, 0), "rapida": (0, 0)}
    with Session() as s:
        assert s.get(Job, "lenta:1") and not s.get(Job, "rapida:1")


def test_wait_for_network_gives_up_without_sleeping_for_real():
    sleeps = []
    assert wait_for_network(60, 15, sleep=sleeps.append, check=lambda: False) is False
    assert sleeps == [15, 15, 15, 15]
    answers = iter([False, True])
    assert wait_for_network(60, 15, sleep=sleeps.append, check=lambda: next(answers)) is True


def test_logsetup_rotates(tmp_path):
    log_file = tmp_path / "logs" / "radar.log"
    logger = setup_logging(log_file, max_bytes=300, backup_count=2)
    for i in range(30):
        logger.info("línea de prueba con tilde número %d", i)
    assert log_file.exists() and (tmp_path / "logs" / "radar.log.1").exists()
    assert "tilde" in log_file.read_text(encoding="utf-8")
    # llamarla otra vez con el mismo archivo no duplica handlers
    n = len(logger.handlers)
    assert len(setup_logging(log_file).handlers) == n
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def test_run_offline_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "wait_for_network", lambda *a, **k: False)
    called = []
    monkeypatch.setattr(run, "ingest", lambda *a, **k: called.append(1))
    log_file = tmp_path / "radar.log"
    assert run.main(["--log-file", str(log_file)]) == 0
    assert not called and "Sin red" in log_file.read_text(encoding="utf-8")
    logger = logging.getLogger("radar")
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
