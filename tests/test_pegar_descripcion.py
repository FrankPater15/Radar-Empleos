"""Pegar la descripción de una oferta de alerta por correo (sin descripción) y evaluarla con IA."""
import pytest
from fastapi.testclient import TestClient

from radar import main
from radar.ingest import upsert_jobs
from radar.models import Job
from radar.scoring import JobScore, ScoringError

from tests.test_search import Session, job  # noqa: F401  (Session es un fixture)

LOCAL = "http://127.0.0.1:8000"
PASTED = """Desarrollador Junior
Experiencia en desarrollo de software utilizando React y .NET (C#).
Conocimientos sólidos en bases de datos, especialmente SQL.
Conocimientos en desarrollo e integración de APIs REST.
Disponibilidad para trabajar en modalidad híbrida en Medellín."""


class FakeScorer:
    cv_hash = "cv1"
    model = "fake-model"

    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.seen = []

    def score(self, j):
        self.seen.append(j.description)
        if self.fail:
            raise self.fail
        return JobScore(score=4, recommendation="postular", summary="Encaja bien.", matched=["React"],
                        missing=[".NET"], red_flags=[], fit_pct=80, blockers=[])


@pytest.fixture
def client(Session, monkeypatch):  # noqa: F811
    def override():
        with Session() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = override
    with Session() as s:
        upsert_jobs(s, [job(id="linkedin:1", source="linkedin", title="Desarrollador Junior", company="Softgic",
                            description="Softgic · Medellín (híbrido)\nOferta de una alerta de LinkedIn."),
                        job(id="getonbrd:1", source="getonbrd", title="QA Junior", company="Otra",
                            description="Descripción completa de la API.")])
    scorer = FakeScorer()
    monkeypatch.setattr(main, "make_panel_scorer", lambda: scorer)
    yield TestClient(main.app, base_url=LOCAL), Session, scorer
    main.app.dependency_overrides.clear()


def test_paste_saves_rescores_and_comes_back_to_the_card(client):
    c, Session, scorer = client  # noqa: F811
    with Session() as s:
        before = s.get(Job, "linkedin:1").fit
    r = c.post("/jobs/linkedin:1/description", follow_redirects=False,
               data={"description": f"  {PASTED}\r\n", "back": "/?source=linkedin&aviso=viejo#oferta-linkedin:1"})
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/?source=linkedin&aviso=") and loc.endswith("#oferta-linkedin:1")
    assert "viejo" not in loc
    assert scorer.seen == [PASTED]  # la IA recibió lo pegado, sin espacios de sobra
    with Session() as s:
        j = s.get(Job, "linkedin:1")
        assert j.description == PASTED and j.description_manual is True
        assert j.fit > before  # React, SQL, REST… ahora cuentan en el prefiltro
        assert j.score == 4 and j.score_data["model"] == "fake-model" and j.score_cv_hash == "cv1"
    page = c.get(loc.split("#")[0]).text
    assert "«Desarrollador Junior»: 4/5, postular." in page
    assert "Descripción pegada por ti" in page


def test_ingest_keeps_the_pasted_description(client):
    c, Session, _ = client  # noqa: F811
    c.post("/jobs/linkedin:1/description", data={"description": PASTED}, follow_redirects=False)
    with Session() as s:
        fit_pasted = s.get(Job, "linkedin:1").fit
        upsert_jobs(s, [job(id="linkedin:1", source="linkedin", title="Desarrollador Junior", company="Softgic",
                            description="Softgic · Medellín (híbrido)\nOferta de una alerta de LinkedIn.")])
        j = s.get(Job, "linkedin:1")
        assert j.description == PASTED and j.fit == fit_pasted and j.score == 4


def test_ai_failure_keeps_description_and_explains(client):
    c, Session, scorer = client  # noqa: F811
    scorer.fail = ScoringError("Gemini falló con linkedin:1: 503 UNAVAILABLE", transient=True)
    r = c.post("/jobs/linkedin:1/description", data={"description": PASTED}, follow_redirects=False)
    assert "no+pudo+evaluarla" in r.headers["location"]
    with Session() as s:
        j = s.get(Job, "linkedin:1")
        assert j.description == PASTED and j.description_manual and j.score is None and j.score_data is None


def test_paste_validates_input(client):
    c, _, scorer = client
    assert c.post("/jobs/linkedin:1/description", data={"description": "   "}).status_code == 400
    assert c.post("/jobs/linkedin:1/description", data={"description": "x" * 20_001}).status_code == 400
    assert c.post("/jobs/linkedin:nope/description", data={"description": "algo"}).status_code == 404
    assert scorer.seen == []


def test_paste_box_only_on_sources_without_description(client):
    c, _, _ = client
    page = c.get("/").text
    assert 'action="/jobs/linkedin:1/description"' in page
    assert 'action="/jobs/getonbrd:1/description"' not in page


def test_with_notice_keeps_filters_and_rejects_foreign_urls():
    assert main.with_notice("/?q=qa#oferta-a:1", "Listo") == "/?q=qa&aviso=Listo#oferta-a:1"
    assert main.with_notice("https://malo.com/x", "Listo") == "/?aviso=Listo"
