"""Utilidades compartidas por las fuentes de ofertas."""
from __future__ import annotations

import html
import re
import time

import httpx

from radar.config import USER_AGENT

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|li|ul|ol|br|div|h\d|tr)[^>]*>", re.I)
_SPACES_RE = re.compile(r"[ \t ]+")

# Países donde una oferta "LATAM" incluye a Colombia
LATAM_WORDS = ("latam", "latin america", "latinoamérica", "latinoamerica", "south america",
               "américa latina", "america latina", "americas")
WORLDWIDE_WORDS = ("anywhere", "worldwide", "global", "cualquier país")


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = _BLOCK_RE.sub("\n", raw)
    text = html.unescape(_TAG_RE.sub("", text))
    lines = [_SPACES_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


_URL_RE = re.compile(r"https?://[^\s/?#]+\S*", re.I)


def safe_url(url) -> str | None:
    """La URL limpia si es http(s); None para javascript:, data:, vacías o no-texto."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    return url if _URL_RE.fullmatch(url) else None


def make_client() -> httpx.Client:
    return httpx.Client(timeout=25, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def get_json(client: httpx.Client, url: str, params: dict | None = None, retries: int = 3,
             sleep=time.sleep):
    """GET con reintentos cortos ante 429/5xx, para no tumbar la ingesta por un fallo puntual."""
    for attempt in range(retries + 1):
        resp = client.get(url, params=params)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            # Si el servidor dice cuánto esperar (Retry-After), le hacemos caso
            wait = to_int(resp.headers.get("retry-after")) or 5 * 2 ** attempt
            sleep(min(wait, 60))
            continue
        resp.raise_for_status()
        return resp.json()


def geo_open_to_colombia(places: list[str] | str | None) -> bool:
    """True si la lista de lugares incluye Colombia, LATAM o 'cualquier lugar'."""
    if not places:
        return False
    items = [places] if isinstance(places, str) else places
    for p in items:
        low = p.lower()
        if "colombia" in low or any(w in low for w in LATAM_WORDS) or any(w in low for w in WORLDWIDE_WORDS):
            return True
    return False


def to_int(v) -> int | None:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n or None
