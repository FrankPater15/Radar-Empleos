"""Lector de las alertas de empleo de LinkedIn que llegan a tu correo (fase 1b).

No visita LinkedIn: solo lee el HTML del correo que ya está en tu bandeja (ver gmail_alerts).
Todas las variantes del correo (alerta de búsqueda, "empleos similares a…", "solicita ya",
empleos guardados) usan la misma tarjeta por oferta:

    <a href=".../jobs/view/ID/..."><img alt="Empresa"></a>
    <a href=".../jobs/view/ID/...">
        <a href=".../jobs/view/ID/...">Título</a>
        Empresa · Lugar (Modalidad)
        ...
    </a>

Una tarjeta es entonces: un texto dentro de un enlace a /jobs/view/ID seguido de inmediato por
"Empresa · Lugar" dentro del mismo enlace. Así se descartan el encabezado ("Empleos similares
a…"), el botón "Solicitar" y el pie del correo, aunque también lleven enlaces o un "·".
"""
from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser

SOURCE = "linkedin"
SOURCE_NAME = "LinkedIn (alertas)"

_JOB = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
_SPACES = re.compile(r"\s+")
_MODE = re.compile(r"\s*\(([^)]*)\)\s*$")
MODES = {"en remoto": "fully_remote", "remoto": "fully_remote", "híbrido": "hybrid", "hibrido": "hybrid",
         "presencial": "no_remote"}
LABELS = {"fully_remote": "remoto", "hybrid": "híbrido", "no_remote": "presencial"}


class _Texts(HTMLParser):
    """Textos del correo en orden, cada uno con la oferta del enlace que lo contiene (o None)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texts: list[tuple[str | None, str]] = []
        self._links: list[str | None] = []  # pila: los enlaces de LinkedIn vienen anidados

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            m = _JOB.search(dict(attrs).get("href") or "")
            self._links.append(m.group(1) if m else None)

    def handle_endtag(self, tag):
        if tag == "a" and self._links:
            self._links.pop()

    def handle_data(self, data):
        text = _SPACES.sub(" ", data).replace("͏", "").strip()  # ͏: relleno invisible del preheader
        if text:
            self.texts.append((self._links[-1] if self._links else None, text))


def _place(raw: str) -> tuple[str, str | None]:
    """'Medellín (Híbrido)' -> ('Medellín', 'hybrid')."""
    m = _MODE.search(raw)
    if not m:
        return raw.strip(), None
    return raw[:m.start()].strip(), MODES.get(m.group(1).strip().lower())


def parse_alert(html: str, received: datetime | None = None) -> list[dict]:
    """Ofertas de un correo de alerta de LinkedIn, sin repetir (una oferta sale en varios enlaces)."""
    p = _Texts()
    p.feed(html or "")
    jobs, seen = [], set()
    for (job_id, title), (next_id, detail) in zip(p.texts, p.texts[1:]):
        if job_id is None or next_id != job_id or job_id in seen or " · " not in detail:
            continue
        seen.add(job_id)
        company, place = (s.strip() for s in detail.split(" · ", 1))
        location, modality = _place(place)
        countries = [location] if "colombia" in location.lower() else [location, "Colombia"]
        lines = [f"{company} · {location}" + (f" ({LABELS[modality]})" if modality else ""),
                 "Oferta de una alerta de LinkedIn en tu correo: la descripción completa está en el enlace."]
        jobs.append({
            "id": f"{SOURCE}:{job_id}",
            "source": SOURCE,
            "external_id": job_id,
            "title": title,
            "company": company or None,
            # Enlace limpio: sin los parámetros de rastreo del correo
            "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
            "category": None,
            "seniority": None,
            "remote_modality": modality,
            "countries": countries,
            "min_salary": None,
            "max_salary": None,
            "salary_currency": None,
            "description": "\n".join(lines),
            "published_at": received,
            # Las alertas salen de tus propias búsquedas en Colombia
            "open_to_colombia": True,
        })
    return jobs
