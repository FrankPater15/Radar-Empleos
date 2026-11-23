"""Prefiltro local (sin IA, gratis e instantáneo): nota de afinidad 0–100 para cada oferta.

Sirve para dos cosas:
  1. ordenar el panel aunque una oferta todavía no tenga nota de IA;
  2. gastar la cuota de Gemini solo en las ofertas que valen la pena (score.py usa MIN_FIT).

Las reglas buscan señales de nivel (junior vs. senior), años de experiencia exigidos,
tipo de rol (técnico o no), habilidades del CV e idioma. Cada ajuste deja una razón
legible, que el panel muestra como etiqueta.
"""
from __future__ import annotations

import re
import unicodedata

from radar import config


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _rx(words: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9])(?:{words})(?![a-z0-9])")


JUNIOR = _rx(r"junior|jr\.?|trainee|intern|internship|pasante|pasantia|practicante|practica|"
             r"aprendiz|entry[- ]level|graduate|recien egresado|sin experiencia|semillero")
SEMI = _rx(r"semi[- ]?senior|ssr|mid[- ]?level|intermedio")
SENIOR = _rx(r"senior|sr\.?|lead|lider|tech lead|staff|principal|head|director|manager|gerente|"
             r"arquitecto|architect|expert|experto|vp")
TECH_ROLE = _rx(r"database|dba|salesforce|mulesoft|sap|erp|crm|security|seguridad|infosec|"
                r"ciberseguridad|machine learning|ml|ai|ia|cloud|desarrollador(?:a)?|developer|dev|engineer|ingenier[oa]|programador(?:a)?|software|"
                r"full[- ]?stack|backend|back[- ]end|frontend|front[- ]end|qa|tester|testing|automation|"
                r"automatizacion|automatizaciones|rpa|soporte|support|implementacion|implementation|"
                r"sistemas|it|ti|web|mobile|flutter|python|java|javascript|react|node|angular|\.net|"
                r"data|datos|devops|integraciones|integration|aplicaciones|technical|tecnico")
NON_TECH = _rx(r"sales|ventas|vendedor|account executive|ejecutiv[oa]|marketing|paid media|media buyer|"
               r"ad ops|ads|seo|recruiter|reclutador|talent|hr|rrhh|people|finance|finanzas|contador|"
               r"accountant|legal|abogado|nurse|clinical|copywriter|content|linguist|linguistic|"
               r"translator|traductor|customer service|call center|telemarketing|designer|disenador|"
               r"product owner|business analyst|analista de negocios|onboarding|success")
# Títulos claramente de desarrollo: ganan aunque el título tenga una palabra "no técnica"
CORE_DEV = _rx(r"desarrollador(?:a|es)?|developers?|programador(?:a|es)?|software engineers?|ingenier[oa] de software|"
               r"full[- ]?stack|backend|back[- ]end|frontend|front[- ]end|qa|automation|automatizacion|rpa")
REQ_CODE = re.compile(r"(?<![a-z0-9])(?:jr|req|r)-?\d{3,}")
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:anos|years?|yrs)")
ENGLISH_HIGH = _rx(r"c1|c2|b2\+?|fluent english|english fluency|fluent in english|advanced english|"
                   r"ingles avanzado|ingles fluido|bilingue|bilingual|native english|"
                   r"excellent english|strong english|full professional proficiency")
SPANISH_POST = _rx(r"requisitos|experiencia|conocimientos|buscamos|ofrecemos|funciones|"
                   r"responsabilidades|desarrollador")
LOCAL = _rx(r"medellin|antioquia|envigado|itagui|sabaneta|bello")


def _skills() -> list[tuple[str, re.Pattern]]:
    return [(s, _rx(re.escape(_norm(s)))) for s in config.SKILLS]


_SKILL_RX = None


def fit(job) -> tuple[int, list[str]]:
    """Devuelve (nota 0–100, razones). `job` puede ser un Job o un dict con los mismos campos."""
    global _SKILL_RX
    if _SKILL_RX is None:
        _SKILL_RX = _skills()
    get = (lambda k: job.get(k)) if isinstance(job, dict) else (lambda k: getattr(job, k, None))
    title = _norm(get("title"))
    body = _norm(get("description"))[:6000]
    seniority = _norm(get("seniority"))
    text = f"{title}\n{body}"
    score, why = 35, []

    def add(points: int, reason: str):
        nonlocal score
        score += points
        why.append(f"{'+' if points > 0 else ''}{points} {reason}")

    # 1. Nivel: el título manda (p. ej. "Junior/Semi Senior" cuenta como junior)
    clean = REQ_CODE.sub(" ", title)          # "JR-186652" es un código, no "junior"
    senior_t = SENIOR.search(SEMI.sub(" ", clean))
    junior_t = JUNIOR.search(clean)
    if junior_t and senior_t:                  # "Junior a Senior": rango amplio
        add(0, "nivel mixto")
    elif junior_t or (not senior_t and seniority in ("junior", "sin experiencia")):
        add(+25, "nivel junior")
    elif senior_t or seniority in ("senior", "expert"):
        add(-35, "nivel senior")
    elif SEMI.search(clean) or seniority == "semi senior":
        add(-5, "semi senior")

    # 2. Años de experiencia pedidos (el mayor número cerca de "años/years")
    years = [int(m.group(1)) for m in YEARS.finditer(body)
             if 0 < int(m.group(1)) < 20 and "experien" in body[max(0, m.start() - 80):m.end() + 80]]
    if years:
        need = max(years) if max(years) <= 10 else min(years)
        if need >= 5:
            add(-30, f"pide {need}+ años")
        elif need >= 3:
            add(-15, f"pide {need}+ años")
        elif need <= 1:
            add(+10, f"pide {need} año{'s' if need != 1 else ''}")

    # 3. Tipo de rol
    if CORE_DEV.search(title):
        add(+15, "rol de desarrollo")
    elif NON_TECH.search(title):
        add(-40, "rol no técnico")
    elif TECH_ROLE.search(title):
        add(+5, "rol técnico")
    else:
        add(-15, "rol poco claro")

    # 4. Habilidades de tu CV que aparecen en la oferta
    hits = [name for name, rx in _SKILL_RX if rx.search(text)]
    if hits:
        add(min(15, 3 * len(hits)), "usa " + ", ".join(hits[:5]))

    # 5. Idioma
    if ENGLISH_HIGH.search(text):
        add(-10, "inglés avanzado")
    if len(SPANISH_POST.findall(body)) >= 2:
        add(+5, "oferta en español")

    # 6. Ubicación
    if LOCAL.search(text):
        add(+10, "Medellín/Antioquia")
    elif "colombia" in text or "Colombia" in (get("countries") or []):
        add(+5, "menciona Colombia")

    return max(0, min(100, score)), why
