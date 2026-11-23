"""Fuente: API pública de Himalayas (empleos remotos), filtrada a ofertas abiertas a Colombia.

Docs: https://himalayas.app/api  ·  Las ofertas enlazan a la página original de Himalayas.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import urlparse

import httpx

from radar.sources.common import geo_open_to_colombia, get_json, html_to_text, make_client, parse_each, to_int

SEARCH_URL = "https://himalayas.app/jobs/api/search"
SOURCE = "himalayas"
SOURCE_NAME = "Himalayas"

# Del más bajo al más alto: nos quedamos con el nivel más bajo que acepte la oferta
LEVELS = [("Entry-level", "Junior"), ("Mid-level", "Semi Senior"), ("Senior", "Senior"),
          ("Manager", "Expert"), ("Director", "Expert"), ("Executive", "Expert")]


def _level(raw: list[str] | None) -> str | None:
    raw = raw or []
    for key, name in LEVELS:
        if key in raw:
            return name
    return None


def _monthly(value, period: str | None) -> int | None:
    n = to_int(value)
    if n is None:
        return None
    return round(n / 12) if (period or "").lower() in ("annual", "yearly", "year") else n


def parse_job(j: dict) -> dict:
    guid = j.get("guid") or j.get("applicationLink") or ""
    path = urlparse(guid).path.strip("/")  # companies/<empresa>/jobs/<slug>
    external_id = path.removeprefix("companies/").replace("/jobs/", "/") or guid
    places = j.get("locationRestrictions") or []
    cats = j.get("parentCategories") or j.get("categories") or []
    pub = to_int(j.get("pubDate"))
    period = j.get("salaryPeriod")
    lo, hi = _monthly(j.get("minSalary"), period), _monthly(j.get("maxSalary"), period)
    return {
        "id": f"{SOURCE}:{external_id}",
        "source": SOURCE,
        "external_id": external_id,
        "title": (j.get("title") or "(sin título)").strip(),
        "company": j.get("companyName"),
        "url": guid or "https://himalayas.app/jobs",
        "category": str(cats[0]).replace("-", " ") if cats else None,
        "seniority": _level(j.get("seniority")),
        "remote_modality": "fully_remote",
        # Sin restricciones = remoto desde cualquier país
        "countries": places or ["Cualquier país"],
        "min_salary": lo,
        "max_salary": hi,
        "salary_currency": (j.get("currency") or "USD") if (lo or hi) else None,
        "description": html_to_text(j.get("description")),
        "published_at": datetime.fromtimestamp(pub, tz=timezone.utc) if pub else None,
        "open_to_colombia": not places or geo_open_to_colombia(places),
    }


def search(query: str, max_pages: int = 3, client: httpx.Client | None = None,
           delay_s: float = 2.0, country: str = "CO") -> Iterator[dict]:
    own_client = client is None
    client = client or make_client()
    try:
        for page in range(1, max_pages + 1):
            payload = get_json(client, SEARCH_URL, {"q": query, "country": country, "page": page})
            if not isinstance(payload, dict):
                break
            jobs = payload.get("jobs") or []
            yield from parse_each(jobs, parse_job, label="himalayas")
            total = to_int(payload.get("totalCount")) or 0
            if not isinstance(jobs, list) or not jobs or (to_int(payload.get("offset")) or 0) + len(jobs) >= total:
                break
            time.sleep(delay_s)
    finally:
        if own_client:
            client.close()


def fetch_all(queries: list[str], max_pages: int, client: httpx.Client | None = None,
              log=print) -> Iterator[dict]:
    for i, q in enumerate(queries):
        if i:
            time.sleep(2.0)  # Himalayas limita la frecuencia de peticiones
        try:
            yield from search(q, max_pages, client)
        except httpx.HTTPError as e:
            log(f"  ! himalayas '{q}': {e}")
