"""Ingesta de ofertas.  Uso:  python -m radar.ingest"""
from __future__ import annotations

import argparse
from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from radar import config
from radar.db import SessionLocal, init_db
from radar.models import Job
from radar.sources import getonbrd

# Campos que la ingesta puede actualizar. status y first_seen_at pertenecen al usuario.
SOURCE_FIELDS = [
    "source", "external_id", "title", "company", "url", "category", "seniority",
    "remote_modality", "countries", "min_salary", "max_salary", "salary_currency",
    "description", "published_at", "open_to_colombia",
]


def _same(a, b) -> bool:
    # SQLite devuelve datetimes sin zona horaria; compáralos sin ella
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a.replace(tzinfo=None) == b.replace(tzinfo=None)
    return a == b


def upsert_jobs(session: Session, jobs: Iterable[dict]) -> tuple[int, int]:
    """Inserta ofertas nuevas y refresca las existentes. Devuelve (nuevas, actualizadas)."""
    new = updated = 0
    seen: set[str] = set()
    for data in jobs:
        if data["id"] in seen:  # la misma oferta puede salir en dos categorías
            continue
        seen.add(data["id"])
        job = session.get(Job, data["id"])
        if job is None:
            session.add(Job(**{k: data[k] for k in ["id", *SOURCE_FIELDS]}))
            new += 1
        else:
            changed = False
            for k in SOURCE_FIELDS:
                if not _same(getattr(job, k), data[k]):
                    setattr(job, k, data[k])
                    changed = True
            updated += changed
    session.commit()
    return new, updated


def run(categories: list[str], max_pages: int, per_page: int) -> None:
    init_db()
    with SessionLocal() as session:
        for cat in categories:
            new, upd = upsert_jobs(
                session, getonbrd.fetch_category(cat, max_pages=max_pages, per_page=per_page)
            )
            print(f"[getonbrd/{cat}] nuevas: {new}  actualizadas: {upd}")


def main() -> None:
    p = argparse.ArgumentParser(description="Trae ofertas de fuentes permitidas a la base de datos.")
    p.add_argument("--categories", default=",".join(config.GETONBRD_CATEGORIES))
    p.add_argument("--max-pages", type=int, default=config.GETONBRD_MAX_PAGES)
    p.add_argument("--per-page", type=int, default=config.GETONBRD_PER_PAGE)
    args = p.parse_args()
    run([c.strip() for c in args.categories.split(",") if c.strip()], args.max_pages, args.per_page)


if __name__ == "__main__":
    main()
