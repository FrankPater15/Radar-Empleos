from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from radar.db import Base

# Embudo: encontrada -> aprobada -> enviada -> entrevista -> oferta | rechazo
# (descartada = la revisaste y no te interesa)
STATUSES = ["encontrada", "aprobada", "enviada", "entrevista", "oferta", "rechazo", "descartada"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "jobs"

    # "<fuente>:<id externo>", p. ej. "getonbrd:desarrollador-backend-...-a1eb"
    id: Mapped[str] = mapped_column(String(300), primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(255))

    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str | None] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500))
    category: Mapped[str | None] = mapped_column(String(100), index=True)
    seniority: Mapped[str | None] = mapped_column(String(50), index=True)
    remote_modality: Mapped[str | None] = mapped_column(String(50))
    countries: Mapped[list] = mapped_column(JSON, default=list)
    min_salary: Mapped[int | None] = mapped_column(Integer)
    max_salary: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(10))
    description: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # Relevancia para Colombia: oferta en Colombia o 100 % remota
    open_to_colombia: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Prefiltro local 0–100 (radar/relevance.py) y sus razones; se recalcula en cada ingesta
    fit: Mapped[int | None] = mapped_column(Integer, index=True)
    fit_reasons: Mapped[list | None] = mapped_column(JSON)
    # título+empresa normalizados: detecta la misma oferta publicada en dos fuentes
    dedupe_key: Mapped[str | None] = mapped_column(String(400), index=True)

    # Puntuación con IA (fase 2). score_cv_hash detecta si cambiaste el CV desde la última nota
    score: Mapped[int | None] = mapped_column(Integer, index=True)
    score_data: Mapped[dict | None] = mapped_column(JSON)
    score_cv_hash: Mapped[str | None] = mapped_column(String(32))
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Campos del usuario: la ingesta nunca los sobrescribe
    status: Mapped[str] = mapped_column(String(20), default="encontrada", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Run(Base):
    """Cada corrida de radar.run: sirve para saber qué ofertas son "nuevas" en el panel."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    new_jobs: Mapped[int | None] = mapped_column(Integer)
    promising: Mapped[int | None] = mapped_column(Integer)
    errors: Mapped[dict | None] = mapped_column(JSON)
