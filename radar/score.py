"""Puntúa con IA las ofertas pendientes.  Uso:  python -m radar.score --limit 20"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from radar import config
from radar.db import SessionLocal, init_db
from radar.models import Job, utcnow
from radar.scoring import ScoringError, make_scorer


def pending_jobs(session: Session, cv_hash: str, limit: int, only_colombia: bool = True,
                 rescore: bool = False, min_fit: int = 0, max_age_days: int | None = None) -> list[Job]:
    """Ofertas sin nota, o con nota de un CV anterior, empezando por las de mejor prefiltro.

    Las descartadas, las que no pasan el prefiltro (fit < min_fit) y las publicadas hace más
    de `max_age_days` (config.MAX_AGE_DAYS; probablemente cerradas) no gastan cuota de IA.
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
    stmt = stmt.order_by(Job.fit.desc().nulls_last(), Job.published_at.desc().nulls_last()).limit(limit)
    return list(session.scalars(stmt))


def score_pending(session: Session, scorer, limit: int, only_colombia: bool = True,
                  rescore: bool = False, log=print, min_fit: int = 0,
                  batch_size: int | None = None) -> tuple[int, int]:
    """Puntúa las pendientes. Si el scorer tiene `score_batch` (Gemini), va por lotes de
    `batch_size` ofertas por llamada (config.SCORING_BATCH_SIZE); si no, de a una."""
    size = config.SCORING_BATCH_SIZE if batch_size is None else batch_size
    jobs = pending_jobs(session, scorer.cv_hash, limit, only_colombia, rescore, min_fit)
    batched = size > 1 and hasattr(scorer, "score_batch")
    chunks = [jobs[i:i + size] for i in range(0, len(jobs), size)] if batched else [[j] for j in jobs]
    ok = failed = 0
    for chunk in chunks:
        if batched:
            results = scorer.score_batch(chunk)
        else:
            try:
                results = [scorer.score(chunk[0])]
            except ScoringError as e:
                results = [e]
        for job, result in zip(chunk, results):
            if isinstance(result, ScoringError):
                log(f"  ! {result}")
                failed += 1
                continue
            _save(job, result, scorer)
            ok += 1
            log(f"  {result.score}/5  [afinidad {job.fit}]  {job.title[:60]}  ({job.company or '?'})")
        session.commit()  # guarda lote por lote: si algo falla a mitad, no se pierde lo hecho
    return ok, failed


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
    p.add_argument("--cv", default=config.CV_PATH)
    p.add_argument("--provider", default=config.SCORING_PROVIDER, help="gemini o anthropic")
    p.add_argument("--min-fit", type=int, default=config.MIN_FIT,
                   help="afinidad mínima del prefiltro (0–100) para gastar IA en una oferta")
    p.add_argument("--batch-size", type=int, default=config.SCORING_BATCH_SIZE,
                   help="ofertas por llamada a Gemini (1 = de a una)")
    args = p.parse_args()
    try:
        score_cli(args.cv, args.provider, args.limit, not args.all_countries, args.rescore, args.min_fit,
                  batch_size=args.batch_size)
    except ScoreSetupError as e:
        sys.exit(str(e))


class ScoreSetupError(RuntimeError):
    """Falta el CV o la key del proveedor: no se puede puntuar."""


def score_cli(cv: str, provider: str, limit: int, only_colombia: bool = True, rescore: bool = False,
              min_fit: int = config.MIN_FIT, log=print, batch_size: int | None = None) -> tuple[int, int]:
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
                                   batch_size=batch_size)
    log(f"Puntuadas: {ok}  fallidas: {failed}")
    return ok, failed


if __name__ == "__main__":
    main()
