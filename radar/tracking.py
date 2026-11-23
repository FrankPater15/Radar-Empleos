"""Corridas, ofertas nuevas y seguimiento de postulaciones."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from radar.models import Job, Run, utcnow

PROMISING_FIT = 60
PROMISING_SCORE = 4


def _naive_utc(dt: datetime) -> datetime:
    """SQLite guarda los datetimes sin zona: compara siempre en UTC sin tzinfo."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def start_run(session: Session, now: datetime | None = None) -> Run:
    run = Run(started_at=now or utcnow())
    session.add(run)
    session.commit()
    return run


def finish_run(session: Session, run: Run, new_jobs: int, promising: int, errors: dict | None = None,
               now: datetime | None = None) -> None:
    run.finished_at = now or utcnow()
    run.new_jobs, run.promising, run.errors = new_jobs, promising, errors or None
    session.commit()


def last_run_start(session: Session) -> datetime | None:
    """Inicio de la última corrida terminada (las "nuevas" son las vistas desde entonces)."""
    return session.scalar(select(Run.started_at).where(Run.finished_at.is_not(None))
                          .order_by(Run.started_at.desc()).limit(1))


def promising_clause():
    return or_(Job.fit >= PROMISING_FIT, Job.score >= PROMISING_SCORE)


def count_new(session: Session, since: datetime, promising_only: bool = False) -> int:
    stmt = select(func.count()).select_from(Job).where(Job.first_seen_at >= _naive_utc(since))
    if promising_only:
        stmt = stmt.where(Job.open_to_colombia.is_(True), Job.status != "descartada", promising_clause())
    return session.scalar(stmt) or 0
