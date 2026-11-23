"""Fuentes nuevas (Himalayas, Jobicy), prefiltro de afinidad, duplicados y filtros del panel."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from radar import main

LOCAL = "http://127.0.0.1:8000"
from radar.db import init_db
from radar.ingest import dedupe_key, refresh_fit, upsert_jobs
from radar.models import Job
from radar.relevance import fit
from radar.score import pending_jobs
from radar.sources import himalayas, jobicy
from radar.sources.common import geo_open_to_colombia, get_json

# Formas reales de las APIs (recortadas)
HIMALAYAS_JOB = {
    "title": "Backend Developer (Junior / Semi Senior)", "companyName": "Selection Book",
    "companySlug": "selection-book", "employmentType": "Full Time",
    "minSalary": "1200", "maxSalary": "1200", "salaryPeriod": "monthly", "currency": "USD",
    "seniority": ["Entry-level", "Mid-level"],
    "locationRestrictions": ["Argentina", "Colombia", "Mexico"],
    "categories": ["Backend-Engineer"], "parentCategories": ["Developer"],
    "description": "<h3>Requisitos</h3><p>Node.js, TypeScript, Git. 1 año de experiencia.</p>",
    "pubDate": "1795109050",
    "guid": "https://himalayas.app/companies/selection-book/jobs/backend-developer-junior-semi-senior",
}
JOBICY_JOB = {
    "id": 163890, "url": "https://jobicy.com/jobs/163890-senior-software-engineer",
    "jobTitle": "Senior Software Engineer", "companyName": "VTEX",
    "jobIndustry": ["Software Engineering"], "jobGeo": "LATAM, Canada", "jobLevel": "Senior",
    "jobDescription": "<p>5+ years of experience. Fluent English.</p>",
    "pubDate": "2026-11-22T16:10:59+00:00", "annualSalaryMin": 60000, "annualSalaryMax": 84000,
}


def job(**kw):
    base = {"id": "x:1", "source": "x", "external_id": "1", "title": "Desarrollador", "company": "ACME",
            "url": "https://example.com", "category": None, "seniority": None, "remote_modality": None,
            "countries": [], "min_salary": None, "max_salary": None, "salary_currency": None,
            "description": "", "published_at": None, "open_to_colombia": True}
    return base | kw


@pytest.fixture
def Session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


# ---------- Fuentes ----------
def test_himalayas_parse():
    j = himalayas.parse_job(HIMALAYAS_JOB)
    assert j["id"] == "himalayas:selection-book/backend-developer-junior-semi-senior"
    assert j["seniority"] == "Junior"  # el nivel más bajo que acepta
    assert j["open_to_colombia"] is True and j["remote_modality"] == "fully_remote"
    assert (j["min_salary"], j["salary_currency"]) == (1200, "USD")
    assert "<" not in j["description"] and j["published_at"].year == 2026


def test_himalayas_without_restrictions_is_worldwide_and_yearly_salary_is_monthly():
    j = himalayas.parse_job(HIMALAYAS_JOB | {"locationRestrictions": [], "minSalary": "24000",
                                             "maxSalary": None, "salaryPeriod": "annual"})
    assert j["open_to_colombia"] is True and j["countries"] == ["Cualquier país"]
    assert j["min_salary"] == 2000


def test_jobicy_parse():
    j = jobicy.parse_job(JOBICY_JOB)
    assert j["id"] == "jobicy:163890" and j["seniority"] == "Senior"
    assert j["countries"] == ["LATAM", "Canada"] and j["open_to_colombia"] is True
    assert (j["min_salary"], j["max_salary"]) == (5000, 7000)
    assert jobicy.parse_job(JOBICY_JOB | {"jobGeo": "Brazil"})["open_to_colombia"] is False
    assert jobicy._level("Entry-Level, Junior") == "Junior"


def test_geo_rules():
    assert geo_open_to_colombia(["Colombia"]) and geo_open_to_colombia("LATAM")
    assert geo_open_to_colombia(["Anywhere"])
    assert not geo_open_to_colombia(["USA", "Canada"]) and not geo_open_to_colombia([])


def test_get_json_retries_then_succeeds():
    class Resp:
        def __init__(self, code, headers=None):
            self.status_code, self.headers = code, headers or {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    calls = iter([Resp(429, {"retry-after": "7"}), Resp(200)])
    client = type("C", (), {"get": lambda self, url, params=None: next(calls)})()
    sleeps = []
    assert get_json(client, "u", sleep=sleeps.append) == {"ok": True}
    assert sleeps == [7]


# ---------- Prefiltro ----------
def test_fit_prefers_junior_dev_roles():
    junior, _ = fit(job(title="Desarrollador Junior Python", description="Requisitos: Python, SQL, Git"))
    senior, why = fit(job(title="Senior Backend Engineer", description="5+ years of experience in Java"))
    sales, _ = fit(job(title="Sales Development Representative Junior"))
    assert junior >= 75 and senior <= 20 and sales < 45
    assert any("senior" in r for r in why) and any("5+ años" in r for r in why)


def test_fit_handles_tricky_titles():
    # "JR-186652" es un código de vacante, no "junior"
    assert "nivel senior" in " ".join(fit(job(title="JR-186652 Full stack Developer Senior"))[1])
    # "Junior / Semi Senior" cuenta como junior
    assert "nivel junior" in " ".join(fit(job(title="Backend Developer (Junior / Semi Senior)"))[1])
    # los años que no hablan de experiencia no penalizan
    assert not any("años" in r for r in fit(job(title="Developer", description="Empresa con 10 años en el mercado"))[1])


def test_fit_scale_is_clamped():
    for title in ["Senior Staff Principal Head", "Junior Python React Node Angular Developer Medellín"]:
        assert 0 <= fit(job(title=title, description="python react node sql git qa rpa medellin " * 3))[0] <= 100


# ---------- Ingesta: prefiltro y duplicados ----------
def test_upsert_computes_fit_and_skips_cross_source_duplicates(Session):
    with Session() as s:
        assert upsert_jobs(s, [job(id="a:1", source="a", title="QA Automation Junior", company="ACME S.A.S.")]) == (1, 0)
        stored = s.get(Job, "a:1")
        assert stored.fit is not None and stored.fit_reasons
        # la misma oferta en otra fuente (mayúsculas/sufijos distintos) no se duplica
        assert upsert_jobs(s, [job(id="b:9", source="b", title="QA automation junior", company="Acme")]) == (0, 0)
        # pero dos vacantes iguales de la misma fuente sí se guardan (p. ej. una por país)
        assert upsert_jobs(s, [job(id="a:2", source="a", title="QA Automation Junior", company="ACME")]) == (1, 0)


def test_dedupe_key_normalizes():
    assert dedupe_key("Desarrollador Júnior (Remoto)", "ACME Inc.") == dedupe_key("desarrollador junior", "acme")


def test_refresh_fit_fills_old_rows(Session):
    with Session() as s:
        s.add(Job(id="old:1", source="old", external_id="1", title="Developer Junior", url="u"))
        s.commit()
        assert refresh_fit(s) == 1 and s.get(Job, "old:1").fit is not None
        assert refresh_fit(s) == 0


def test_pending_jobs_respects_min_fit_and_orders_by_fit(Session):
    with Session() as s:
        upsert_jobs(s, [job(id="a:1", source="a", title="Senior Architect", company="A"),
                        job(id="a:2", source="a", title="Desarrollador Junior Python", company="B",
                            description="Python, SQL"),
                        job(id="a:3", source="a", title="Developer", company="C")])
        ids = [j.id for j in pending_jobs(s, "h", 10, min_fit=45)]
        assert ids[0] == "a:2" and "a:1" not in ids


# ---------- Panel ----------
@pytest.fixture
def client(Session):
    def override():
        with Session() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = override
    with Session() as s:
        upsert_jobs(s, [job(id="himalayas:x", source="himalayas", title="Desarrollador Junior Python", company="H",
                            description="Python, SQL, Git"),
                        job(id="jobicy:y", source="jobicy", title="Senior Staff Engineer", company="J")])
    yield TestClient(main.app, base_url=LOCAL)
    main.app.dependency_overrides.clear()


def test_panel_filters_by_source_and_fit(client):
    html = client.get("/").text
    assert "Desarrollador Junior Python" in html and "Senior Staff Engineer" in html
    assert "Senior Staff Engineer" not in client.get("/?source=himalayas").text
    assert "Senior Staff Engineer" not in client.get("/?min_fit=60").text
    assert "Himalayas" in html  # nombre legible de la fuente
    api = client.get("/api/jobs?order=afinidad").json()
    assert api[0]["id"] == "himalayas:x" and api[0]["fit"] >= api[1]["fit"]


def test_undo_button_and_anchor_redirect(client):
    r = client.post("/jobs/himalayas:x/status", data={"status": "enviada", "back": "/?q=python#job-1"},
                    follow_redirects=False)
    assert r.headers["location"] == "/?q=python#job-1"
    html = client.get("/?status=enviada").text
    assert "Deshacer" in html
    assert client.post("/jobs/himalayas:x/status", data={"status": "aprobada", "back": "/\\evil.com"},
                       follow_redirects=False).headers["location"] == "/"
