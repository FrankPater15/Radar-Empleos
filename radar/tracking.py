"""Corridas, ofertas nuevas y seguimiento de postulaciones."""
from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, TextIO

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from radar.models import Job, Run, utcnow

PROMISING_FIT = 60
PROMISING_SCORE = 4
FOLLOW_UP_DAYS = int(os.getenv("FOLLOW_UP_DAYS", "7"))
NEW_FALLBACK = timedelta(hours=24)  # sin corridas registradas, "nuevas" = últimas 24 h
PRE_SEND = ("encontrada", "aprobada")
CLOSES_FOLLOW_UP = ("entrevista", "oferta", "rechazo", "descartada")


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


def new_since(session: Session, now: datetime | None = None) -> datetime:
    """Desde cuándo una oferta cuenta como "nueva" en el panel (?nuevas=1)."""
    return last_run_start(session) or (now or utcnow()) - NEW_FALLBACK


# ---------- Seguimiento ----------
def apply_status(job: Job, new: str, now: datetime | None = None) -> None:
    """Cambia el estado y mantiene las fechas de seguimiento.

    - "enviada" por primera vez: applied_at = ahora y follow_up_at = ahora + FOLLOW_UP_DAYS.
    - Volver a encontrada/aprobada (p. ej. deshacer): se borran applied_at y follow_up_at.
    - Entrevista, oferta, rechazo o descartada: ya no hay seguimiento pendiente.
    """
    now = now or utcnow()
    if new == job.status:
        return
    job.status = new
    job.status_changed_at = now
    if new == "enviada":
        if job.applied_at is None:
            job.applied_at = now
            job.follow_up_at = now + timedelta(days=FOLLOW_UP_DAYS)
    elif new in PRE_SEND:
        job.applied_at = job.follow_up_at = None
    elif new in CLOSES_FOLLOW_UP:
        job.follow_up_at = None


def follow_up_due_clause(now: datetime | None = None):
    return and_(Job.status == "enviada", Job.follow_up_at.is_not(None),
                Job.follow_up_at <= _naive_utc(now or utcnow()))


def count_follow_ups(session: Session, now: datetime | None = None) -> int:
    return session.scalar(select(func.count()).select_from(Job).where(follow_up_due_clause(now))) or 0


def follow_up(job: Job, action: str, now: datetime | None = None) -> None:
    """Acción "hecho": ya hiciste el seguimiento (se quita el recordatorio). "posponer": +FOLLOW_UP_DAYS."""
    now = now or utcnow()
    if action == "hecho":
        job.follow_up_at = None
    elif action == "posponer":
        job.follow_up_at = now + timedelta(days=FOLLOW_UP_DAYS)
    else:
        raise ValueError(action)


# ---------- Exportar ----------
CSV_HEADERS = ["estado", "titulo", "empresa", "fuente", "url", "afinidad", "nota_ia", "recomendacion",
               "publicada", "vista", "enviada", "seguimiento", "notas"]


def _d(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _safe(v) -> str:
    """Neutraliza fórmulas (CSV injection): Excel ejecuta celdas que empiezan con = + - @."""
    s = "" if v is None else str(v)
    return "'" + s if s.startswith(_FORMULA_START) else s


def export_csv(jobs: Iterable[Job], fp: TextIO, source_names: dict[str, str] | None = None) -> int:
    """CSV con ';' (Excel en español lo abre en columnas). La codificación (utf-8-sig) la pone quien escribe."""
    source_names = source_names or {}
    w = csv.writer(fp, delimiter=";", lineterminator="\r\n")
    w.writerow(CSV_HEADERS)
    n = 0
    for j in jobs:
        rec = (j.score_data or {}).get("recommendation", "") if isinstance(j.score_data, dict) else ""
        w.writerow([_safe(c) for c in (
            j.status, j.title, j.company or "", source_names.get(j.source, j.source), j.url,
            "" if j.fit is None else j.fit, "" if j.score is None else j.score, rec,
            _d(j.published_at), _d(j.first_seen_at), _d(j.applied_at), _d(j.follow_up_at),
            j.notes or "")])
        n += 1
    return n
