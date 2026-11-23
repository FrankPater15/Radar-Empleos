"""Fuente: API pública de Jobicy (empleos remotos), región LATAM.

Docs: https://jobicy.com/jobs-rss-feed  ·  Jobicy pide citarlo como fuente y enlazar
siempre a la URL original de la oferta: el panel muestra "Jobicy" y abre esa URL.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Iterator

import httpx

from radar.sources.common import geo_open_to_colombia, get_json, html_to_text, make_client, parse_each, to_int

API_URL = "https://jobicy.com/api/v2/remote-jobs"
SOURCE = "jobicy"
SOURCE_NAME = "Jobicy"

LEVELS = {"entry-level": "Junior", "junior": "Junior", "midweight": "Semi Senior",
          "senior": "Senior", "director": "Expert"}


def _level(raw: str | None) -> str | None:
    for part in (raw or "").lower().replace(",", " ").split():
        if part in LEVELS:
            return LEVELS[part]  # "Entry-Level, Junior" -> Junior
    return None


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    if isinstance(v, str) and v.startswith("["):
        return v.strip("[]").split(",")[0].strip(" '\"") or None
    return v


def parse_job(j: dict) -> dict:
    geo = [g.strip() for g in (j.get("jobGeo") or "").split(",") if g.strip()]
    pub = j.get("pubDate")
    try:
        published = datetime.fromisoformat(pub) if pub else None
    except (ValueError, TypeError):
        published = None
    lo, hi = to_int(j.get("annualSalaryMin")), to_int(j.get("annualSalaryMax"))
    lo, hi = (round(lo / 12) if lo else None), (round(hi / 12) if hi else None)
    return {
        "id": f"{SOURCE}:{j['id']}",
        "source": SOURCE,
        "external_id": str(j["id"]),
        "title": (j.get("jobTitle") or "(sin título)").strip(),
        "company": j.get("companyName"),
        "url": j.get("url") or "https://jobicy.com",
        "category": _first(j.get("jobIndustry")),
        "seniority": _level(j.get("jobLevel")),
        "remote_modality": "fully_remote",
        "countries": geo,
        "min_salary": lo,
        "max_salary": hi,
        "salary_currency": (j.get("salaryCurrency") or "USD") if (lo or hi) else None,
        "description": html_to_text(j.get("jobDescription")),
        "published_at": published,
        "open_to_colombia": geo_open_to_colombia(geo),
    }


def fetch_all(industries: list[str], client: httpx.Client | None = None, log=print,
              delay_s: float = 1.0) -> Iterator[dict]:
    own_client = client is None
    client = client or make_client()
    try:
        for ind in industries:
            try:
                payload = get_json(client, API_URL, {"count": 100, "geo": "latam", "industry": ind})
            except httpx.HTTPError as e:
                log(f"  ! jobicy/{ind}: {e}")
                continue
            if isinstance(payload, dict):
                yield from parse_each(payload.get("jobs"), parse_job, label=f"jobicy/{ind}")
            time.sleep(delay_s)
    finally:
        if own_client:
            client.close()
