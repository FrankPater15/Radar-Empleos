"""Lector del correo semanal "Perfect Match" de Computrabajo (fase 1b).

No visita Computrabajo: solo lee el HTML del correo. Los demás correos (empresas destacadas,
"te buscan para nuevos empleos") solo enlazan a búsquedas, sin ofertas: dan una lista vacía.

Orden de los textos: "de la semana" · título · [valoración] · empresa · lugar · detalles… ·
enlace "Postúlate" · "Descripción de la oferta" · extracto · enlace "Ver oferta".
"""
from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

SOURCE = "computrabajo"
SOURCE_NAME = "Computrabajo (alertas)"

_ID = re.compile(r"-([0-9A-Fa-f]{16,})$")
_RATING = re.compile(r"\d(?:[.,]\d)?")
_SPACES = re.compile(r"\s+")


def _offer(href: str) -> tuple[str, str] | None:
    """(id, URL limpia) si el enlace, directo o por go.computrabajo.com, lleva a una oferta."""
    parts = urlsplit(href)
    if parts.hostname == "go.computrabajo.com":
        parts = urlsplit((parse_qs(parts.query).get("url") or [""])[0])
    host = parts.hostname or ""
    m = _ID.search(parts.path)
    if not m or not parts.path.startswith("/ofertas-de-trabajo/"):
        return None
    if host != "computrabajo.com" and not host.endswith(".computrabajo.com"):
        return None
    return m.group(1).upper(), f"https://{host}{parts.path}"


class _Reader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []
        self.offer: tuple[str, str] | None = None
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip = True
        elif tag == "a" and self.offer is None:
            self.offer = _offer(dict(attrs).get("href") or "")

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip = False

    def handle_data(self, data):
        text = _SPACES.sub(" ", data).strip()
        if text and not self._skip:
            self.texts.append(text)


def _modality(details: list[str]) -> str | None:
    for d in (x.lower() for x in details):
        onsite, remote = "presencial" in d, "remoto" in d
        if (onsite and remote) or "híbrido" in d:
            return "hybrid"
        if remote:
            return "fully_remote"
        if onsite:
            return "no_remote"
    return None


def parse_alert(html: str, received: datetime | None = None) -> list[dict]:
    p = _Reader()
    p.feed(html or "")
    if p.offer is None or "de la semana" not in p.texts or "Postúlate" not in p.texts:
        return []
    start = p.texts.index("de la semana") + 1
    head = [t for t in p.texts[start:p.texts.index("Postúlate")] if not _RATING.fullmatch(t)]
    if len(head) < 3:
        return []
    title, company, place, *details = head
    excerpt = []
    if "Descripción de la oferta" in p.texts:
        for t in p.texts[p.texts.index("Descripción de la oferta") + 1:]:
            if t == "Ver oferta":
                break
            excerpt.append(t)
    job_id, url = p.offer
    return [{
        "id": f"{SOURCE}:{job_id}", "source": SOURCE, "external_id": job_id,
        "title": title, "company": company, "url": url, "category": None, "seniority": None,
        "remote_modality": _modality(details), "countries": [place, "Colombia"],
        # El salario va en pesos y el panel muestra US$: se deja en el texto
        "min_salary": None, "max_salary": None, "salary_currency": None,
        "description": "\n".join([f"{company} · {place}", *details, *excerpt]),
        "published_at": received, "open_to_colombia": True,
    }]
