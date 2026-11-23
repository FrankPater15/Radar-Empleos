"""Ranking v2: reglas del prefiltro, extracción de requisitos, rúbrica, lotes y rank_value."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors

from radar import config
from radar.models import Job, utcnow
from radar.relevance import extract_requirements, fit, rank_value
from radar.score import pending_jobs, score_pending
from radar.scoring import (GeminiScorer, JobScore, compact_description, cv_hash, system_prompt)

from tests.test_scoring import gemini_client, js, session  # noqa: F401  (fixture)
from tests.test_search import job

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def reasons(j):
    return " ".join(fit(j)[1])


# ---------- Reglas v2 del prefiltro ----------
def test_ai_trainer_language_roles_are_penalized():
    score, why = fit(job(title="Korean Language Data Contributor – Freelance AI Trainer", seniority="Junior"))
    assert score < 45 and "anotación" in " ".join(why)


def test_country_restricted_offer_penalized():
    j = job(title="Full Stack Developer Intern",
            description="This job is a remote position. You can work anywhere in the Philippines.\n"
                        "Job opening exclusive only to Filipino locals")
    assert "restringida a Filipinas" in reasons(j)
    assert extract_requirements(j)["pais_restringido"] == "Filipinas"


def test_country_restriction_ignores_latam():
    j = job(description="Candidates must be based in LATAM (Colombia, Mexico).")
    assert extract_requirements(j)["pais_restringido"] is None


def test_years_range_uses_minimum():
    j = job(title="Desarrollador", description="Requisitos: 1-3 años de experiencia en Python")
    assert extract_requirements(j)["anos_min"] == 1 and extract_requirements(j)["anos_max"] == 3
    assert "pide 1 año" in reasons(j) and "3+" not in reasons(j)


def test_years_in_words_and_months():
    assert extract_requirements(job(description="Mínimo dos años de experiencia"))["anos_min"] == 2
    assert extract_requirements(job(description="6 meses de experiencia en QA"))["anos_min"] == 0


def test_multilevel_posting_counts_as_junior():
    desc = ("Seniority Levels\nJunior\n0–2 years of experience.\nMid\n2–5 years of experience.\n"
            "Senior\n5+ years of experience.\nFlutter, Dart, Git")
    score, why = fit(job(title="Software Engineer - Flutter", description=desc))
    assert score >= 45 and not any("años" in w for w in why)
    assert extract_requirements(job(description=desc))["multinivel"] is True


def test_associate_and_entry_level_count_as_junior():
    assert "associate" in reasons(job(title="Associate Linux Support Engineer"))
    assert "entry-level" in reasons(job(title="Software Engineer", description="This is an entry-level role"))


def test_assistant_coordinator_not_tech():
    assert fit(job(title="Junior Recruitment Coordinator"))[0] < 45
    assert "asistente" in reasons(job(title="Virtual Assistant"))


@pytest.mark.parametrize("title", [
    "Junior Associate Counsel, Commercial Transactions",
    "Compliance Intern",
    "Payroll & Benefits Intern (Practicante de Nómina y Compensación)",
    "Construction Assistant",
    "Intern TA Volumen",
])
def test_legal_payroll_recruiting_construction_not_tech(title):
    score, why = fit(job(title=title, seniority="Junior"))
    assert "rol no técnico" in " ".join(why)
    assert score < config.MIN_FIT


@pytest.mark.parametrize("title", [
    "QA Engineer", "Desarrollador Frontend", "Associate Linux Support Engineer",
    "Software Engineer - Python and K8s", "Data Engineer",
])
def test_tech_titles_unaffected_by_v3_words(title):
    assert "no técnico" not in reasons(job(title=title))


def test_dev_title_with_sales_word_still_dev():
    assert "rol de desarrollo" in reasons(job(title="Desarrollador CRM Ventas"))


def test_english_levels():
    assert extract_requirements(job(description="Fluent English required"))["ingles"] == "alto"
    assert extract_requirements(job(description="English level: B1–B2"))["ingles"] == "intermedio"
    assert extract_requirements(job(description="Upper-intermediate English"))["ingles"] == "intermedio"
    assert "-20 pide inglés alto (CV A2)" in reasons(job(description="Advanced English"))
    assert "-8 pide inglés intermedio" in reasons(job(description="Good English skills"))
    assert "inglés deseable" in reasons(job(description="Inglés deseable"))


def test_student_only_internship(monkeypatch):
    j = job(title="Practicante de desarrollo", description="Debe ser estudiante activo de ingeniería")
    assert "exige ser estudiante" in reasons(j)
    monkeypatch.setattr(config, "IS_STUDENT", True)
    assert "estudiante" not in reasons(j)


def test_scam_negation_ignored():
    legit = job(description="We will never ask for payment for training. We never contact via WhatsApp or Telegram.")
    assert extract_requirements(legit)["alertas_estafa"] == []
    scam = job(description="Debes pagar el kit de capacitación. Contacto solo por WhatsApp. Escribe a rrhh.jobs@gmail.com")
    alerts = extract_requirements(scam)["alertas_estafa"]
    assert len(alerts) == 3 and "-50 posible estafa" in reasons(scam)


def test_extract_modality_schedule_salary():
    req = extract_requirements(job(remote_modality="hybrid", description="Night shift. Salary: $1.5K - $1.7K USD"))
    assert req["modalidad"] == "híbrido" and req["horario"] == "nocturno"
    assert req["salario"] == {"min": 1500, "max": 1700, "moneda": "USD", "fuente": "texto"}
    assert "horario nocturno" in req["bloqueadores"]
    portal = extract_requirements(job(min_salary=1000, max_salary=2000, salary_currency="USD"))
    assert portal["salario"]["fuente"] == "portal"
    json.dumps(req)  # serializable para score_data / API


# ---------- rank_value ----------
def test_rank_prefers_scored_good_over_unscored_high_fit():
    good = job(score=4, score_data={"fit_pct": 80}, fit=70, published_at=NOW)
    unscored = job(fit=90, published_at=NOW)
    assert rank_value(good, NOW) > rank_value(unscored, NOW)
    assert rank_value(job(score=3, score_data={}, published_at=NOW), NOW) == 50


def test_old_offers_decay():
    fresh = job(fit=80, published_at=NOW - timedelta(days=3))
    old = job(fit=80, published_at=(NOW - timedelta(days=74)).replace(tzinfo=None))  # naive de SQLite
    assert rank_value(fresh, NOW) == 64 and rank_value(old, NOW) == pytest.approx(16, abs=0.1)


def test_blockers_push_down():
    clean = job(score=4, score_data={"fit_pct": 75}, published_at=NOW)
    blocked = job(score=4, score_data={"fit_pct": 75, "blockers": ["inglés C1"]}, published_at=NOW,
                  description="Must be authorized to work in the US. Fluent English.")
    assert rank_value(blocked, NOW) == 75 - 30 < rank_value(clean, NOW)


# ---------- Rúbrica v2 ----------
def test_system_prompt_mentions_english_level_and_non_dev_cap():
    p = system_prompt("Mi CV")
    assert "inglés A2" in p and "máxima es 2" in p and "la nota es 1" in p and "fit_pct" in p


def test_cv_hash_includes_rubric_version(monkeypatch):
    before = cv_hash("cv")
    monkeypatch.setattr(config, "RUBRIC_VERSION", "v3")
    assert cv_hash("cv") != before


def test_old_score_data_still_parses():
    old = {"score": 2, "recommendation": "no postular", "summary": "x", "matched": [], "missing": [], "red_flags": []}
    s = JobScore.model_validate(old)
    assert s.fit_pct == 0 and s.blockers == []


# ---------- Lotes ----------
def batch_json(*items):
    return json.dumps({"items": [{**js(score).model_dump(), "id": i, "fit_pct": 10 * score} for i, score in items]})


def jobs(n):
    return [Job(id=f"x:{i}", title=f"Dev {i}", description="Python") for i in range(n)]


def test_batch_maps_ids_back_to_jobs():
    client = gemini_client(batch_json(("j2", 2), ("j1", 5), ("j3", 3)))
    scorer = GeminiScorer("Mi CV", client=client, fallback_model="", rpm=0)
    out = scorer.score_batch(jobs(3))
    assert [r.score for r in out] == [5, 2, 3] and out[0].fit_pct == 50
    call = client.models.calls[0]
    assert call["contents"].count("<oferta id=") == 3 and "Mi CV" not in call["contents"]
    assert "items" in json.dumps(call["config"]["response_json_schema"])
    assert call["config"]["system_instruction"].count("Mi CV") == 1


def test_batch_missing_item_falls_back_to_single():
    client = gemini_client(batch_json(("j1", 4), ("j9", 1)), js(3).model_dump_json())
    scorer = GeminiScorer("cv", client=client, fallback_model="", rpm=0)
    out = scorer.score_batch(jobs(2))
    assert [r.score for r in out] == [4, 3] and len(client.models.calls) == 2
    assert "<oferta id=" not in client.models.calls[1]["contents"]  # reintento individual


def test_batch_invalid_json_retries_all_individually():
    client = gemini_client("no es json", js(2).model_dump_json(), "tampoco")
    out = GeminiScorer("cv", client=client, fallback_model="", rpm=0).score_batch(jobs(2))
    assert out[0].score == 2 and isinstance(out[1], Exception)


def test_score_pending_uses_batches(session):  # noqa: F811
    client = gemini_client(batch_json(("j1", 4), ("j2", 2)))
    scorer = GeminiScorer("cv", client=client, fallback_model="", rpm=0)
    assert score_pending(session, scorer, limit=10, log=lambda *_: None, batch_size=6) == (2, 0)
    assert len(client.models.calls) == 1
    assert session.get(Job, "getonbrd:a").score_data["fit_pct"] == 40


def test_rpm_budget_waits_before_exceeding():
    t = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s

    client = gemini_client(*[js(3).model_dump_json()] * 3)
    scorer = GeminiScorer("cv", client=client, fallback_model="", rpm=2, sleep=sleep, clock=lambda: t[0])
    for _ in range(3):
        scorer.score(Job(id="x", title="t"))
    assert sleeps == [60]


def test_fallback_retries_primary_every_n_calls():
    busy = genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "x"}})
    ok = js(3).model_dump_json()
    client = gemini_client(busy, ok, ok, ok, ok)
    scorer = GeminiScorer("cv", client=client, model="main", fallback_model="lite", rpm=0,
                          primary_retry_every=3, log=lambda *_: None)
    for _ in range(4):
        scorer.score(Job(id="x", title="t"))
    assert [c["model"] for c in client.models.calls] == ["main", "lite", "lite", "lite", "main"]


def test_compact_description_keeps_requirements_drops_benefits():
    text = ("About us\n" + "We are a great company. " * 40 + "\nRequirements\nPython and SQL\n"
            "3+ years of experience\nBenefits\n" + "Free snacks and gym. " * 40 + "\nEnglish B2 required")
    out = compact_description(text, limit=400)
    assert len(out) <= 400 and "Python and SQL" in out and "3+ years" in out and "English B2" in out
    assert "snacks" not in out and "great company" not in out


# ---------- Antigüedad ----------
def test_pending_skips_old_offers(session):  # noqa: F811
    session.get(Job, "getonbrd:a").published_at = utcnow() - timedelta(days=120)
    session.commit()
    assert [j.id for j in pending_jobs(session, "h", 10)] == ["getonbrd:b"]
    assert len(pending_jobs(session, "h", 10, max_age_days=0)) == 2
