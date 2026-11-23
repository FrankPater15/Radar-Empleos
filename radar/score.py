"""Puntúa con IA las ofertas pendientes.  Uso:  python -m radar.score --limit 20"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from radar import config
from radar.db import SessionLocal, init_db
from radar.models import Job, utcnow
from radar.scoring import Scorer, ScoringError


def pending_jobs(session: Session, cv_hash: str, limit: int, only_colombia: bool = True,
                 rescore: bool = False) -> list[Job]:
    """Ofertas sin nota, o con nota de un CV anterior. Las descartadas no gastan tokens."""
    stmt = select(Job).where(Job.status != "descartada")
    if only_colombia:
        stmt = stmt.where(Job.open_to_colombia.is_(True))
    if not rescore:
        stmt = stmt.where(or_(Job.score.is_(None), Job.score_cv_hash != cv_hash))
    stmt = stmt.order_by(Job.published_at.desc().nulls_last()).limit(limit)
    return list(session.scalars(stmt))


def score_pending(session: Session, scorer: Scorer, limit: int, only_colombia: bool = True,
                  rescore: bool = False, log=print) -> tuple[int, int]:
    ok = failed = 0
    for job in pending_jobs(session, scorer.cv_hash, limit, only_colombia, rescore):
        try:
            result = scorer.score(job)
        except ScoringError as e:
            log(f"  ! {e}")
            failed += 1
            continue
        job.score = result.score
        job.score_data = result.model_dump()
        job.score_cv_hash = scorer.cv_hash
        job.scored_at = utcnow()
        session.commit()  # guarda una por una: si algo falla a mitad, no se pierde lo hecho
        ok += 1
        log(f"  {result.score}/5  {job.title[:60]}  ({job.company or '?'})")
    return ok, failed


def main() -> None:
    p = argparse.ArgumentParser(description="Puntúa ofertas contra tu CV con Claude.")
    p.add_argument("--limit", type=int, default=20, help="máximo de ofertas a puntuar (controla el costo)")
    p.add_argument("--all-countries", action="store_true", help="incluir ofertas no abiertas a Colombia")
    p.add_argument("--rescore", action="store_true", help="volver a puntuar aunque ya tengan nota")
    p.add_argument("--cv", default=config.CV_PATH)
    args = p.parse_args()

    cv_path = Path(args.cv)
    if not cv_path.exists():
        sys.exit(f"No encuentro el CV en {cv_path}. Copia cv/cv_maestro.example.md a {cv_path} y complétalo.")

    import anthropic

    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("Falta ANTHROPIC_API_KEY. Créala en https://platform.claude.com y ponla en .env")

    init_db()
    scorer = Scorer(cv_path.read_text(encoding="utf-8"))
    with SessionLocal() as session:
        try:
            ok, failed = score_pending(session, scorer, args.limit, not args.all_countries, args.rescore)
        except anthropic.AuthenticationError:
            sys.exit("Falta la API key de Anthropic o no es válida: define ANTHROPIC_API_KEY en .env")
    print(f"Puntuadas: {ok}  fallidas: {failed}")


if __name__ == "__main__":
    main()
