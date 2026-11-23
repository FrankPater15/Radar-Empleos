"""Seguimiento de postulaciones: fechas, notas, filtros nuevas/seguimiento y exportar CSV."""
import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from radar import main
from radar.db import init_db
from radar.ingest import upsert_jobs
from radar.models import Job
from radar.tracking import (apply_status, count_follow_ups, export_csv, finish_run, follow_up,
                            start_run)

from tests.test_search import job

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def Session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_apply_status_enviada_sets_follow_up():
    j = Job(id="a:1", status="aprobada")
    apply_status(j, "enviada", now=NOW)
    assert j.applied_at == NOW and j.follow_up_at == NOW + timedelta(days=7)
    assert j.status_changed_at == NOW
    # entrevista: ya no hay seguimiento pendiente, pero se conserva la fecha de envío
    apply_status(j, "entrevista", now=NOW + timedelta(days=2))
    assert j.applied_at == NOW and j.follow_up_at is None
    # volver a "enviada" no reinicia la fecha de envío
    apply_status(j, "enviada", now=NOW + timedelta(days=3))
    assert j.applied_at == NOW


def test_undo_clears_applied_dates():
    j = Job(id="a:1", status="encontrada")
    apply_status(j, "enviada", now=NOW)
    apply_status(j, "encontrada", now=NOW)
    assert j.applied_at is None and j.follow_up_at is None


def test_follow_up_actions_and_due_count(Session):
    with Session() as s:
        upsert_jobs(s, [job(id="a:1", source="a", title="QA Junior", company="A")])
        j = s.get(Job, "a:1")
        apply_status(j, "enviada", now=NOW - timedelta(days=8))
        s.commit()
        assert count_follow_ups(s) == 1
        follow_up(j, "posponer")
        s.commit()
        assert count_follow_ups(s) == 0
        follow_up(j, "hecho")
        assert j.follow_up_at is None
        with pytest.raises(ValueError):
            follow_up(j, "otra")


def test_ingest_never_overwrites_tracking_fields(Session):
    with Session() as s:
        upsert_jobs(s, [job(id="a:1", source="a", title="QA Junior", company="A")])
        j = s.get(Job, "a:1")
        apply_status(j, "enviada", now=NOW)
        j.notes = "Hablé con María de RR. HH."
        s.commit()
        assert upsert_jobs(s, [job(id="a:1", source="a", title="QA Junior (actualizada)", company="A")]) == (0, 1)
        j = s.get(Job, "a:1")
        assert j.notes == "Hablé con María de RR. HH." and j.status == "enviada"
        assert j.applied_at is not None and j.follow_up_at is not None


def test_export_csv_roundtrip():
    j = Job(id="a:1", source="himalayas", title="Analista QA", company="Ñandú S.A.S.", url="https://x",
            status="enviada", fit=72, score=4, score_data={"recommendation": "postular"},
            first_seen_at=NOW, applied_at=NOW, follow_up_at=NOW + timedelta(days=7),
            notes="Llamar el lunes; pidió portafolio")
    buf = io.StringIO()
    assert export_csv([j], buf, {"himalayas": "Himalayas"}) == 1
    rows = list(csv.reader(io.StringIO(buf.getvalue()), delimiter=";"))
    assert rows[0][:3] == ["estado", "titulo", "empresa"] and rows[0][-1] == "notas"
    assert rows[1][:4] == ["enviada", "Analista QA", "Ñandú S.A.S.", "Himalayas"]
    assert rows[1][-3:] == ["2026-11-23", "2026-11-30", "Llamar el lunes; pidió portafolio"]


# ---------- Panel ----------
@pytest.fixture
def client(Session):
    def override():
        with Session() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = override
    with Session() as s:
        upsert_jobs(s, [job(id="a:vieja", source="a", title="Oferta vieja", company="V")])
        s.get(Job, "a:vieja").first_seen_at = datetime.now(timezone.utc) - timedelta(days=3)
        s.commit()
        run = start_run(s, now=datetime.now(timezone.utc) - timedelta(hours=1))
        upsert_jobs(s, [job(id="a:nueva", source="a", title="Oferta nueva QA", company="N")])
        finish_run(s, run, 1, 0)
    yield TestClient(main.app), Session
    main.app.dependency_overrides.clear()


def test_nuevas_filter_uses_last_run(client):
    c, _ = client
    html = c.get("/?nuevas=1").text
    assert "Oferta nueva QA" in html and "Oferta vieja" not in html
    assert "Nueva</span>" in c.get("/").text  # insignia en la tarjeta


def test_marking_enviada_creates_follow_up_and_filter(client):
    c, Session = client
    c.post("/jobs/a:vieja/status", data={"status": "enviada"})
    with Session() as s:
        j = s.get(Job, "a:vieja")
        assert j.applied_at is not None and j.follow_up_at is not None
        j.follow_up_at = datetime.now(timezone.utc) - timedelta(days=1)  # ya venció
        s.commit()
    html = c.get("/?seguimiento=1").text
    assert "Oferta vieja" in html and "Oferta nueva QA" not in html
    assert "Seguimiento hecho" in html
    r = c.post("/jobs/a:vieja/follow-up", data={"action": "hecho", "back": "/?seguimiento=1"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?seguimiento=1"
    assert "Oferta vieja" not in c.get("/?seguimiento=1").text
    assert c.post("/jobs/a:vieja/follow-up", data={"action": "borrar"}).status_code == 400


def test_notes_endpoint(client):
    c, Session = client
    r = c.post("/jobs/a:nueva/notes", data={"notes": "  Referida por Ana  ", "back": "/#job-1"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#job-1"
    with Session() as s:
        assert s.get(Job, "a:nueva").notes == "Referida por Ana"
    assert "Referida por Ana" in c.get("/").text
    c.post("/jobs/a:nueva/notes", data={"notes": ""})
    with Session() as s:
        assert s.get(Job, "a:nueva").notes is None
    assert c.post("/jobs/a:nueva/notes", data={"notes": "x" * 6000}).status_code == 400
    assert c.post("/jobs/a:nope/notes", data={"notes": "x"}).status_code == 404


def test_export_csv_endpoint_respects_filters(client):
    c, _ = client
    r = c.get("/export.csv?nuevas=1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert r.content.startswith(b"\xef\xbb\xbf")  # utf-8-sig para Excel
    text = r.content.decode("utf-8-sig")
    assert "Oferta nueva QA" in text and "Oferta vieja" not in text
    assert "Oferta vieja" in c.get("/export.csv").content.decode("utf-8-sig")
    assert 'href="/export.csv?colombia=1&amp;nuevas=1"' in c.get("/?nuevas=1").text


# ---------- Atajos / cambio de estado por fetch ----------
def test_status_json_for_fetch(client):
    c, Session = client
    r = c.post("/jobs/a:nueva/status", data={"status": "enviada"}, headers={"Accept": "application/json"})
    assert r.status_code == 200
    body = r.json()
    assert body["previous"] == "encontrada" and body["job"]["status"] == "enviada"
    assert body["job"]["applied_at"] and body["job"]["follow_up_at"] and body["follow_up"]
    assert body["counts"]["by_status"]["enviada"] == 1 and body["counts"]["follow_ups"] == 0
    # deshacer con fetch: vuelve al estado anterior y limpia las fechas
    body = c.post("/jobs/a:nueva/status", data={"status": "encontrada"},
                  headers={"Accept": "application/json"}).json()
    assert body["job"]["applied_at"] is None and body["counts"]["by_status"]["enviada"] == 0
    bad = c.post("/jobs/a:nueva/status", data={"status": "x"}, headers={"Accept": "application/json"})
    assert bad.status_code == 400 and "inválido" in bad.json()["detail"]
    # sin Accept JSON sigue redirigiendo (los formularios funcionan sin JS)
    assert c.post("/jobs/a:nueva/status", data={"status": "aprobada"}, follow_redirects=False).status_code == 303


def test_notes_json_for_fetch(client):
    c, _ = client
    r = c.post("/jobs/a:nueva/notes", data={"notes": "ok"}, headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json()["job"]["notes"] == "ok"


def test_panel_has_shortcut_hooks(client):
    c, _ = client
    html = c.get("/").text
    assert "keys.js" in html and 'id="keys-help"' in html and 'role="status"' in html
    assert 'data-status="encontrada"' in html and 'tabindex="-1"' in html
    assert 'data-count="enviada"' in html
    js = c.get("/static/keys.js")
    assert js.status_code == 200
    for key in ['"j"', '"k"', '"u"', '"o"', '"?"', 'a: "aprobada"', 'd: "descartada"', 'e: "enviada"']:
        assert key in js.text, key
