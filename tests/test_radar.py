import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine

from radar import main
from radar.db import init_db
from radar.ingest import upsert_jobs
from radar.models import Job
from radar.sources.getonbrd import html_to_text, parse_job

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "getonbrd_page.json").read_text(encoding="utf-8"))


@pytest.fixture
def Session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def sample(**overrides):
    base = parse_job(FIXTURE["data"][0])
    base.update(overrides)
    return base


def test_html_to_text_strips_tags_and_entities():
    assert html_to_text("<ul><li>Uno &amp; dos</li><li><strong>Tres</strong></li></ul>") == "Uno & dos\nTres"
    assert html_to_text(None) == ""


def test_parse_real_api_item():
    for item in FIXTURE["data"]:
        job = parse_job(item)
        assert job["id"] == f"getonbrd:{item['id']}"
        assert job["url"].startswith("https://www.getonbrd.com/jobs/")
        assert job["title"]
        assert "<" not in job["description"]


def test_open_to_colombia_rule():
    item = json.loads(json.dumps(FIXTURE["data"][0]))
    item["attributes"]["countries"] = ["Colombia"]
    item["attributes"]["remote_modality"] = "no_remote"
    assert parse_job(item)["open_to_colombia"] is True
    item["attributes"]["countries"] = ["Chile"]
    item["attributes"]["remote_modality"] = "fully_remote"
    assert parse_job(item)["open_to_colombia"] is True
    item["attributes"]["remote_modality"] = "hybrid"
    assert parse_job(item)["open_to_colombia"] is False


def test_upsert_keeps_user_status(Session):
    with Session() as s:
        assert upsert_jobs(s, [sample()]) == (1, 0)
        s.get(Job, sample()["id"]).status = "aprobada"
        s.commit()
        # misma oferta sin cambios -> no cuenta como actualizada
        assert upsert_jobs(s, [sample()]) == (0, 0)
        # cambia el título en la fuente -> se actualiza, el estado se mantiene
        assert upsert_jobs(s, [sample(title="Nuevo título")]) == (0, 1)
        job = s.get(Job, sample()["id"])
        assert job.title == "Nuevo título" and job.status == "aprobada"


def test_upsert_dedupes_within_batch(Session):
    with Session() as s:
        assert upsert_jobs(s, [sample(), sample()]) == (1, 0)


@pytest.fixture
def client(Session):
    def override():
        with Session() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = override
    with Session() as s:
        upsert_jobs(s, [sample(open_to_colombia=True, title="Analista de Soporte TI"),
                        sample(id="getonbrd:otra", external_id="otra", open_to_colombia=False, title="Backend Chile")])
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_index_and_filters(client):
    html = client.get("/").text
    assert "Analista de Soporte TI" in html and "Backend Chile" not in html
    assert "Backend Chile" in client.get("/?colombia=0").text
    assert "Analista" not in client.get("/?q=zzz-no-existe").text


def test_status_change_and_discard(client):
    job_id = sample()["id"]
    r = client.post(f"/jobs/{job_id}/status", data={"status": "descartada", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "Analista de Soporte TI" not in client.get("/").text
    assert client.get("/api/jobs?status=descartada").json()[0]["status"] == "descartada"


def test_status_rejects_bad_input(client):
    job_id = sample()["id"]
    assert client.post(f"/jobs/{job_id}/status", data={"status": "hackeada"}).status_code == 400
    assert client.post("/jobs/getonbrd:nope/status", data={"status": "aprobada"}).status_code == 404
    r = client.post(f"/jobs/{job_id}/status", data={"status": "aprobada", "back": "//evil.com"}, follow_redirects=False)
    assert r.headers["location"] == "/"
