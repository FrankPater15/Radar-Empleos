"""Fuente: API pública oficial de Get on Board (sin autenticación).

Docs: https://www.getonbrd.com/api-doc
Salarios en USD mensuales, según publica Get on Board.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Iterator

import httpx

from radar.sources.common import get_json, html_to_text, make_client  # noqa: F401  (html_to_text se reexporta)

BASE_URL = "https://www.getonbrd.com/api/v0"
SOURCE = "getonbrd"
SOURCE_NAME = "Get on Board"

# GET /api/v0/seniorities
SENIORITIES = {1: "Sin experiencia", 2: "Junior", 3: "Semi Senior", 4: "Senior", 5: "Expert"}


def parse_job(item: dict, category_fallback: str | None = None) -> dict:
    """Convierte un elemento `data[]` de la API en un dict listo para el modelo Job."""
    a = item["attributes"]
    company = (a.get("company") or {}).get("data") or {}
    seniority_id = ((a.get("seniority") or {}).get("data") or {}).get("id")
    countries = a.get("countries") or []
    modality = a.get("remote_modality")
    published = a.get("published_at")

    sections = [
        ("", a.get("description")),
        (a.get("functions_headline") or "Funciones:", a.get("functions")),
        (a.get("desirable_headline") or "Se valora:", a.get("desirable")),
    ]
    description = "\n\n".join(
        f"{head}\n{body}".strip() for head, raw in sections if (body := html_to_text(raw))
    )

    return {
        "id": f"{SOURCE}:{item['id']}",
        "source": SOURCE,
        "external_id": item["id"],
        "title": a.get("title") or "(sin título)",
        "company": (company.get("attributes") or {}).get("name"),
        "url": (item.get("links") or {}).get("public_url") or f"https://www.getonbrd.com/jobs/{item['id']}",
        "category": a.get("category_name") or category_fallback,
        "seniority": SENIORITIES.get(int(seniority_id)) if seniority_id else None,
        "remote_modality": modality,
        "countries": countries,
        "min_salary": a.get("min_salary"),
        "max_salary": a.get("max_salary"),
        "salary_currency": "USD" if (a.get("min_salary") or a.get("max_salary")) else None,
        "description": description,
        "published_at": datetime.fromtimestamp(published, tz=timezone.utc) if published else None,
        "open_to_colombia": "Colombia" in countries or modality == "fully_remote",
    }


def _paginate(url: str, params: dict, max_pages: int, client: httpx.Client | None,
              delay_s: float, category_fallback: str | None) -> Iterator[dict]:
    own_client = client is None
    client = client or make_client()
    try:
        for page in range(1, max_pages + 1):
            payload = get_json(client, url, {**params, "page": page, "expand": '["company"]'})
            for item in payload.get("data", []):
                yield parse_job(item, category_fallback=category_fallback)
            if page >= (payload.get("meta") or {}).get("total_pages", page):
                break
            time.sleep(delay_s)
    finally:
        if own_client:
            client.close()


def fetch_category(category: str, max_pages: int = 5, per_page: int = 50,
                   client: httpx.Client | None = None, delay_s: float = 0.5) -> Iterator[dict]:
    """Recorre las páginas más recientes de una categoría, con pausa entre peticiones."""
    yield from _paginate(f"{BASE_URL}/categories/{category}/jobs", {"per_page": per_page},
                         max_pages, client, delay_s, category)


def search(query: str, max_pages: int = 2, per_page: int = 100,
           client: httpx.Client | None = None, delay_s: float = 0.5) -> Iterator[dict]:
    """Búsqueda de texto libre (encuentra ofertas junior de categorías que no recorremos)."""
    yield from _paginate(f"{BASE_URL}/search/jobs", {"query": query, "per_page": per_page},
                         max_pages, client, delay_s, None)


def fetch_all(categories: list[str], queries: list[str], max_pages: int, per_page: int,
              client: httpx.Client | None = None, log=print) -> Iterator[dict]:
    for cat in categories:
        try:
            yield from fetch_category(cat, max_pages, per_page, client)
        except httpx.HTTPError as e:
            log(f"  ! getonbrd/{cat}: {e}")
    for q in queries:
        try:
            yield from search(q, client=client)
        except httpx.HTTPError as e:
            log(f"  ! getonbrd búsqueda '{q}': {e}")
