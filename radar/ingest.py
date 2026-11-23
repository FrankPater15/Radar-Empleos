"""Ingesta de ofertas desde todas las fuentes.  Uso:  python -m radar.ingest [--sources himalayas]"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from datetime import datetime
from typing import Iterable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar import config
from radar.db import SessionLocal, init_db
from radar.models import Job
from radar.relevance import fit
from radar.sources import getonbrd, himalayas, jobicy
from radar.sources.common import make_client

# Campos que la ingesta puede actualizar. status y first_seen_at pertenecen al usuario.
SOURCE_FIELDS = [
    "source", "external_id", "title", "company", "url", "category", "seniority",
    "remote_modality", "countries", "min_salary", "max_salary", "salary_currency",
    "description", "published_at", "open_to_colombia",
]
DERIVED_FIELDS = ["fit", "fit_reasons", "dedupe_key"]

_NOISE = re.compile(r"\(.*?\)|\[.*?\]|[^a-z0-9 ]")
# Sufijos legales al final del nombre de la empresa ("ACME S.A.S." == "Acme")
_SUFFIXES = {"inc", "llc", "ltd", "sas", "sa", "spa", "srl", "corp", "co", "gmbh", "s", "a", "c", "l", "r"}


def _norm(s: str | None) -> list[str]:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _NOISE.sub(" ", s).split()


def dedupe_key(title: str | None, company: str | None) -> str:
    words = _norm(company)
    while words and words[-1] in _SUFFIXES:
        words.pop()
    return f"{' '.join(_norm(title))}|{' '.join(words)}"


def _same(a, b) -> bool:
    # SQLite devuelve datetimes sin zona horaria; compáralos sin ella
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a.replace(tzinfo=None) == b.replace(tzinfo=None)
    return a == b


def _with_derived(data: dict) -> dict:
    score, reasons = fit(data)
    return {**data, "fit": score, "fit_reasons": reasons,
            "dedupe_key": dedupe_key(data.get("title"), data.get("company"))}


def upsert_jobs(session: Session, jobs: Iterable[dict]) -> tuple[int, int]:
    """Inserta ofertas nuevas y refresca las existentes. Devuelve (nuevas, actualizadas).

    Si una oferta nueva ya existe en otra fuente (mismo título y empresa), se omite.
    """
    new = updated = 0
    seen: set[str] = set()
    for raw in jobs:
        if raw["id"] in seen:  # la misma oferta puede salir en dos categorías o búsquedas
            continue
        seen.add(raw["id"])
        data = _with_derived(raw)
        job = session.get(Job, data["id"])
        if job is None:
            dup = session.scalar(select(Job.id).where(Job.dedupe_key == data["dedupe_key"],
                                                      Job.source != data["source"]).limit(1))
            if dup:
                continue
            session.add(Job(**{k: data[k] for k in ["id", *SOURCE_FIELDS, *DERIVED_FIELDS]}))
            session.flush()  # para que el control de duplicados vea esta oferta
            new += 1
        else:
            changed = False
            for k in SOURCE_FIELDS + DERIVED_FIELDS:
                if not _same(getattr(job, k), data[k]):
                    setattr(job, k, data[k])
                    changed = k in SOURCE_FIELDS or changed
            updated += changed
    session.commit()
    return new, updated


def refresh_fit(session: Session) -> int:
    """Recalcula el prefiltro de todas las ofertas (p. ej. tras cambiar SKILLS o las reglas)."""
    n = 0
    for job in session.scalars(select(Job)):
        score, reasons = fit(job)
        key = dedupe_key(job.title, job.company)
        if (job.fit, job.fit_reasons, job.dedupe_key) != (score, reasons, key):
            job.fit, job.fit_reasons, job.dedupe_key = score, reasons, key
            n += 1
    session.commit()
    return n


def source_streams(sources: list[str], client, log=print) -> Iterator[tuple[str, Iterable[dict]]]:
    for name in sources:
        if name == "getonbrd":
            yield name, getonbrd.fetch_all(config.GETONBRD_CATEGORIES, config.SEARCH_QUERIES,
                                           config.GETONBRD_MAX_PAGES, config.GETONBRD_PER_PAGE,
                                           client, log)
        elif name == "himalayas":
            yield name, himalayas.fetch_all(config.SEARCH_QUERIES, config.HIMALAYAS_MAX_PAGES, client, log)
        elif name == "jobicy":
            yield name, jobicy.fetch_all(config.JOBICY_INDUSTRIES, client, log)
        else:
            log(f"  ! fuente desconocida: {name}")


def _guarded(stream: Iterable[dict], errors: list[BaseException]) -> Iterator[dict]:
    """Corta el flujo de una fuente al primer error, sin perder lo ya descargado."""
    try:
        yield from stream
    except Exception as e:  # red caída, JSON inválido, cambio en la API…
        errors.append(e)


def run(sources: list[str], log=print, session_factory=None, client_factory=make_client,
        errors: dict[str, str] | None = None) -> dict[str, tuple[int, int]]:
    """Ingesta de todas las fuentes. Cada fuente falla por separado: se registra y se sigue.

    Si se pasa `errors`, se llena con {fuente: mensaje} de las que fallaron.
    """
    if session_factory is None:
        init_db()
        session_factory = SessionLocal
    errors = {} if errors is None else errors
    results = {}
    with session_factory() as session, client_factory() as client:
        refresh_fit(session)  # ofertas viejas sin prefiltro
        for name, stream in source_streams(sources, client, log):
            log(f"[{name}] descargando…")
            failures: list[BaseException] = []
            try:
                results[name] = upsert_jobs(session, _guarded(stream, failures))
            except Exception as e:  # error al guardar: se deshace solo esta fuente
                session.rollback()
                failures.append(e)
                results[name] = (0, 0)
            if failures:
                e = failures[0]
                errors[name] = f"{type(e).__name__}: {e}"
                log(f"[{name}] ! error, se sigue con las demás fuentes: {errors[name]}")
            log(f"[{name}] nuevas: {results[name][0]}  actualizadas: {results[name][1]}")
    return results


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Trae ofertas de fuentes permitidas a la base de datos.")
    p.add_argument("--sources", default=",".join(config.SOURCES), help="getonbrd,himalayas,jobicy")
    p.add_argument("--refit", action="store_true", help="solo recalcular el prefiltro, sin descargar")
    args = p.parse_args()
    if args.refit:
        init_db()
        with SessionLocal() as s:
            print(f"Prefiltro recalculado en {refresh_fit(s)} ofertas")
        return
    run([s.strip() for s in args.sources.split(",") if s.strip()])


if __name__ == "__main__":
    main()
