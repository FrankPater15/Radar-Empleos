from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from radar.db import init_db
from radar.ingest import upsert_jobs
from radar.models import Job
from radar.score import pending_jobs, score_pending
from radar.scoring import JobScore, Scorer, ScoringError, job_prompt

from tests.test_radar import sample


class FakeMessages:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        r = self.results.pop(0)
        if isinstance(r, SimpleNamespace):
            return r
        return SimpleNamespace(stop_reason="end_turn", parsed_output=r)


def fake_client(*results):
    return SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(results)))


def js(score, **kw):
    return JobScore(score=score, recommendation=kw.get("rec", "postular"), summary="ok",
                    matched=["Java"], missing=[], red_flags=[])


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        upsert_jobs(s, [
            sample(id="getonbrd:a", external_id="a", open_to_colombia=True, title="A"),
            sample(id="getonbrd:b", external_id="b", open_to_colombia=True, title="B"),
            sample(id="getonbrd:c", external_id="c", open_to_colombia=False, title="C"),
        ])
        yield s


def test_request_shape_uses_cached_cv_and_fallbacks():
    client = fake_client(js(4))
    scorer = Scorer("Mi CV: Java, JWT", client=client, model="claude-opus-5-5", effort="low")
    job = Job(**{k: v for k, v in sample().items()})
    assert scorer.score(job).score == 4
    call = client.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_format"] is JobScore
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert "Mi CV: Java, JWT" in call["system"][0]["text"]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    # la oferta va en el mensaje, no en el system (para que el caché del CV se reutilice)
    assert job.title in call["messages"][0]["content"]
    assert "Mi CV" not in call["messages"][0]["content"]


def test_score_is_clamped_and_refusal_raises():
    client = fake_client(js(9), SimpleNamespace(stop_reason="refusal", parsed_output=None))
    scorer = Scorer("cv", client=client)
    job = Job(**sample())
    assert scorer.score(job).score == 5
    with pytest.raises(ScoringError):
        scorer.score(job)


def test_empty_cv_rejected():
    with pytest.raises(ValueError):
        Scorer("   ", client=fake_client())


def test_score_pending_only_colombia_and_skips_scored(session):
    scorer = Scorer("cv v1", client=fake_client(js(4), js(2)))
    assert score_pending(session, scorer, limit=10, log=lambda *_: None) == (2, 0)
    assert session.get(Job, "getonbrd:c").score is None  # no abierta a Colombia
    assert {session.get(Job, i).score for i in ("getonbrd:a", "getonbrd:b")} == {4, 2}
    # segunda pasada con el mismo CV: nada pendiente
    assert pending_jobs(session, scorer.cv_hash, 10) == []
    # si cambias el CV, todo vuelve a quedar pendiente
    assert len(pending_jobs(session, Scorer("cv v2", client=fake_client()).cv_hash, 10)) == 2


def test_score_pending_keeps_going_after_failure(session):
    refusal = SimpleNamespace(stop_reason="refusal", parsed_output=None)
    scorer = Scorer("cv", client=fake_client(refusal, js(3)))
    assert score_pending(session, scorer, limit=10, log=lambda *_: None) == (1, 1)


def test_discarded_jobs_are_not_scored(session):
    session.get(Job, "getonbrd:a").status = "descartada"
    session.commit()
    ids = [j.id for j in pending_jobs(session, "h", 10)]
    assert ids == ["getonbrd:b"]


def test_job_prompt_contains_key_fields():
    p = job_prompt(Job(**sample(title="QA Junior", seniority="Junior")))
    assert "QA Junior" in p and "Junior" in p


def test_migration_adds_score_columns_to_old_table():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as c:  # tabla de la fase 1, sin columnas de puntuación
        c.execute(text("CREATE TABLE jobs (id VARCHAR PRIMARY KEY, title VARCHAR)"))
    init_db(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert {"score", "score_data", "score_cv_hash", "scored_at", "status"} <= cols


# ---------- Gemini ----------
from google.genai import errors as genai_errors

from radar.scoring import GeminiScorer, make_scorer


class FakeGeminiModels:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(text=r, candidates=[SimpleNamespace(finish_reason="STOP")])


def gemini_client(*results):
    return SimpleNamespace(models=FakeGeminiModels(results))


def test_gemini_request_and_parse():
    client = gemini_client(js(5).model_dump_json())
    scorer = GeminiScorer("Mi CV", client=client, model="gemini-3.8-flash")
    assert scorer.score(Job(**sample())).score == 5
    call = client.models.calls[0]
    assert call["model"] == "gemini-3.8-flash"
    assert "Mi CV" in call["config"]["system_instruction"]
    assert call["config"]["response_mime_type"] == "application/json"
    assert "Mi CV" not in call["contents"]


def test_gemini_retries_rate_limit_then_succeeds():
    rate = genai_errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "x"}})
    sleeps = []
    scorer = GeminiScorer("cv", client=gemini_client(rate, js(3).model_dump_json()), fallback_model="", sleep=sleeps.append)
    assert scorer.score(Job(**sample())).score == 3
    assert sleeps == [10]


def test_gemini_bad_output_and_auth_error_raise_scoring_error():
    auth = genai_errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "key"}})
    scorer = GeminiScorer("cv", client=gemini_client("no es json", None, auth), fallback_model="", sleep=lambda s: None)
    for _ in range(3):
        with pytest.raises(ScoringError):
            scorer.score(Job(**sample()))


def test_gemini_retries_network_drop():
    sleeps = []
    scorer = GeminiScorer("cv", client=gemini_client(ConnectionError("Server disconnected"), js(4).model_dump_json()),
                          fallback_model="", sleep=sleeps.append)
    assert scorer.score(Job(**sample())).score == 4 and sleeps == [5]
    down = [ConnectionError("x")] * 4
    with pytest.raises(ScoringError):
        GeminiScorer("cv", client=gemini_client(*down), fallback_model="", sleep=lambda s: None).score(Job(**sample()))


def test_make_scorer_rejects_unknown_provider():
    with pytest.raises(ValueError):
        make_scorer("cv", "openai")


def test_gemini_switches_to_fallback_on_503():
    busy = genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "x"}})
    client = gemini_client(busy, js(4).model_dump_json(), js(2).model_dump_json())
    scorer = GeminiScorer("cv", client=client, model="gemini-3.8-flash", fallback_model="gemini-3.1-flash-lite",
                          sleep=lambda s: pytest.fail("no debe esperar"), log=lambda *_: None)
    assert scorer.score(Job(**sample())).score == 4
    assert scorer.score(Job(**sample())).score == 2  # se queda con el respaldo
    assert [c["model"] for c in client.models.calls] == ["gemini-3.8-flash", "gemini-3.1-flash-lite", "gemini-3.1-flash-lite"]
