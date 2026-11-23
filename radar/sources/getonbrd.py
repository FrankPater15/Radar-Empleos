"""Fuente: API pública oficial de Get on Board (sin autenticación).

Docs: https://www.getonbrd.com/api-doc
Salarios en USD mensuales, según publica Get on Board.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timezone
from typing import Iterator

import httpx

from radar.config import USER_AGENT

BASE_URL = "https://www.getonbrd.com/api/v0"
SOURCE = "getonbrd"

# GET /api/v0/seniorities
SENIORITIES = {1: "Sin experiencia", 2: "Junior", 3: "Semi Senior", 4: "Senior", 5: "Expert"}

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|li|ul|ol|br|div|h\d)[^>]*>", re.I)
_SPACES_RE = re.compile(r"[ \t]+")


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = _BLOCK_RE.sub("\n", raw)
    text = html.unescape(_TAG_RE.sub("", text))
    lines = [_SPACES_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


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


def fetch_category(
    category: str,
    max_pages: int = 5,
    per_page: int = 50,
    client: httpx.Client | None = None,
    delay_s: float = 0.5,
) -> Iterator[dict]:
    """Recorre las páginas más recientes de una categoría, con pausa entre peticiones."""
    own_client = client is None
    client = client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})
    try:
        for page in range(1, max_pages + 1):
            resp = client.get(
                f"{BASE_URL}/categories/{category}/jobs",
                params={"per_page": per_page, "page": page, "expand": '["company"]'},
            )
            resp.raise_for_status()
            payload = resp.json()
            for item in payload.get("data", []):
                yield parse_job(item, category_fallback=category)
            if page >= (payload.get("meta") or {}).get("total_pages", page):
                break
            time.sleep(delay_s)
    finally:
        if own_client:
            client.close()
