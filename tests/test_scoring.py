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
