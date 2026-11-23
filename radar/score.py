"""Puntúa con IA las ofertas pendientes.  Uso:  python -m radar.score --limit 20"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from radar import config
from radar.db import SessionLocal, init_db
from radar.models import Job, utcnow
from radar.scoring import ScoringError, as_scoring_error, make_scorer


def _gave_up(job: Job, cv_hash: str) -> bool:
    """Oferta "veneno": falló config.SCORE_MAX_FAILURES veces con este CV (ver `_record_failure`)."""
    data = job.score_data
    return (isinstance(data, dict) and data.get("score_failed_cv") == cv_hash
            and (data.get("score_failures") or 0) >= config.SCORE_MAX_FAILURES)


def _record_failure(job: Job, cv_hash: str) -> None:
    """Cuenta un fallo propio de la oferta (no de red/cuota) dentro de score_data, sin nota."""
    data = dict(job.score_data) if isinstance(job.score_data, dict) else {}
    same_cv = data.get("score_failed_cv") == cv_hash
    data.update(score_failures=(data.get("score_failures") or 0) + 1 if same_cv else 1,
                score_failed_cv=cv_hash, score_failed_at=utcnow().isoformat())
    job.score_data = data


def pending_jobs(session: Session, cv_hash: str, limit: int, only_colombia: bool = True,
                 rescore: bool = False, min_fit: int = 0, max_age_days: int | None = None,
                 retry_failed: bool = False) -> list[Job]:
    """Ofertas sin nota, o con nota de un CV anterior, empezando por las de mejor prefiltro.

    Las descartadas, las que no pasan el prefiltro (fit < min_fit) y las publicadas hace más
    de `max_age_days` (config.MAX_AGE_DAYS; probablemente cerradas) no gastan cuota de IA.
    Las que ya fallaron config.SCORE_MAX_FAILURES veces con este CV tampoco (salvo `retry_failed`).
    """
    max_age = config.MAX_AGE_DAYS if max_age_days is None else max_age_days
    stmt = select(Job).where(Job.status != "descartada")
    if max_age:
        stmt = stmt.where(or_(Job.published_at.is_(None), Job.published_at >= utcnow() - timedelta(days=max_age)))
    if only_colombia:
        stmt = stmt.where(Job.open_to_colombia.is_(True))
    if min_fit:
        stmt = stmt.where(Job.fit >= min_fit)
    if not rescore:
        stmt = stmt.where(or_(Job.score.is_(None), Job.score_cv_hash != cv_hash))
    stmt = stmt.order_by(Job.fit.desc().nulls_last(), Job.published_at.desc().nulls_last())
    out: list[Job] = []
    result = session.scalars(stmt.execution_options(yield_per=200))
    try:
        for job in result:
            if retry_failed or not _gave_up(job, cv_hash):
                out.append(job)
                if len(out) >= limit:
                    break
    finally:
        result.close()
    return out


def score_pending(session: Session, scorer, limit: int, only_colombia: bool = True,
                  rescore: bool = False, log=print, min_fit: int = 0,
                  batch_size: int | None = None, retry_failed: bool = False) -> tuple[int, int]:
    """Puntúa las pendientes. Si el scorer tiene `score_batch` (Gemini), va por lotes de
    `batch_size` ofertas por llamada (config.SCORING_BATCH_SIZE); si no, de a una."""
    size = config.SCORING_BATCH_SIZE if batch_size is None else batch_size
    jobs = pending_jobs(session, scorer.cv_hash, limit, only_colombia, rescore, min_fit,
                        retry_failed=retry_failed)
    batched = size > 1 and hasattr(scorer, "score_batch")
    chunks = [jobs[i:i + size] for i in range(0, len(jobs), size)] if batched else [[j] for j in jobs]
    ok = failed = 0
    by_model: Counter[str] = Counter()
    for chunk in chunks:
        try:
            results = scorer.score_batch(chunk) if batched else [scorer.score(chunk[0])]
        except Exception as e:  # cualquier fallo (no solo ScoringError) pierde este lote, no la corrida
            results = [as_scoring_error(e)] * len(chunk)
        for job, result in zip(chunk, results):
            if isinstance(result, Exception):
                err = as_scoring_error(result)
                log(f"  ! {err}")
                failed += 1
                if not err.transient:
                    _record_failure(job, scorer.cv_hash)
                continue
            _save(job, result, scorer)
            ok += 1
            by_model[scorer.model] += 1
            log(f"  {result.score}/5  [afinidad {job.fit}]  {job.title[:60]}  ({job.company or '?'})")
        session.commit()  # guarda lote por lote: si algo falla a mitad, no se pierde lo hecho
    _report_fallback(by_model, getattr(scorer, "primary_model", None), log)
    return ok, failed


def _report_fallback(by_model: Counter[str], primary: str | None, log) -> None:
    """Avisa si parte de las notas salió del modelo de respaldo (más barato y menos fino)."""
    backup = {m: n for m, n in by_model.items() if primary and m != primary}
    if backup:
        used = ", ".join(f"{n} con {m}" for m, n in backup.items())
        log(f"  ! {used}: {primary} no estuvo disponible, esas notas pueden ser menos finas")


def _save(job: Job, result, scorer) -> None:
    job.score = result.score
    job.score_data = {**result.model_dump(), "model": scorer.model}
    job.score_cv_hash = scorer.cv_hash
    job.scored_at = utcnow()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # tildes en la consola de Windows
    p = argparse.ArgumentParser(description="Puntúa ofertas contra tu CV con IA (Gemini o Claude).")
    p.add_argument("--limit", type=int, default=20, help="máximo de ofertas a puntuar (controla el costo)")
    p.add_argument("--all-countries", action="store_true", help="incluir ofertas no abiertas a Colombia")
    p.add_argument("--rescore", action="store_true", help="volver a puntuar aunque ya tengan nota")
    p.add_argument("--retry-failed", action="store_true",
                   help="reintentar también las ofertas que ya fallaron varias veces con este CV")
    p.add_argument("--cv", default=config.CV_PATH)
    p.add_argument("--provider", default=config.SCORING_PROVIDER, help="gemini o anthropic")
    p.add_argument("--min-fit", type=int, default=config.MIN_FIT,
                   help="afinidad mínima del prefiltro (0–100) para gastar IA en una oferta")
    p.add_argument("--batch-size", type=int, default=config.SCORING_BATCH_SIZE,
                   help="ofertas por llamada a Gemini (1 = de a una)")
    args = p.parse_args()
    try:
        score_cli(args.cv, args.provider, args.limit, not args.all_countries, args.rescore, args.min_fit,
                  batch_size=args.batch_size, retry_failed=args.retry_failed)
    except ScoreSetupError as e:
        sys.exit(str(e))


class ScoreSetupError(RuntimeError):
    """Falta el CV o la key del proveedor: no se puede puntuar."""


def score_cli(cv: str, provider: str, limit: int, only_colombia: bool = True, rescore: bool = False,
              min_fit: int = config.MIN_FIT, log=print, batch_size: int | None = None,
              retry_failed: bool = False) -> tuple[int, int]:
    cv_path = Path(cv)
    if not cv_path.exists():
        raise ScoreSetupError(f"No encuentro el CV en {cv_path}. Copia cv/cv_maestro.example.md a {cv_path} y complétalo.")

    provider = provider.lower()
    if provider == "gemini" and not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        raise ScoreSetupError("Falta GEMINI_API_KEY. Créala gratis en https://aistudio.google.com y ponla en .env")
    if provider in ("anthropic", "claude") and not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        raise ScoreSetupError("Falta ANTHROPIC_API_KEY. Créala en https://platform.claude.com y ponla en .env")

    init_db()
    scorer = make_scorer(cv_path.read_text(encoding="utf-8"), provider)
    log(f"Proveedor: {provider} · modelo: {scorer.model}")
    with SessionLocal() as session:
        ok, failed = score_pending(session, scorer, limit, only_colombia, rescore, log=log, min_fit=min_fit,
                                   batch_size=batch_size, retry_failed=retry_failed)
    log(f"Puntuadas: {ok}  fallidas: {failed}")
    return ok, failed


if __name__ == "__main__":
    main()
