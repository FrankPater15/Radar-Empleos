"""Fase 2b: CV adaptado por oferta, en PDF.  Uso:  python -m radar.cv_adapt <id de la oferta> [--html]

La IA elige, ordena y redacta lo que YA está en tu CV maestro para destacar lo que pide la
oferta. Regla dura: nunca inventar experiencia. Además del prompt, `verify()` borra en código
toda habilidad, proyecto, empresa o cifra que no aparezca en el CV maestro.

Los datos de contacto no pasan por la IA: salen de `cv/contacto.md` (líneas "Clave: valor").
El PDF lo imprime Edge o Chrome en modo headless; queda en `cv/adaptados/` (Git lo ignora).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from radar import config
from radar.models import Job
from radar.scoring import ScoringError, _job_block, as_scoring_error

HERE = Path(__file__).parent
OUT_DIR = config.project_path(os.getenv("CV_OUT_DIR", "cv/adaptados"))
CONTACT_PATH = config.project_path(os.getenv("CV_CONTACT_PATH", "cv/contacto.md"))


class Project(BaseModel):
    name: str
    stack: str = ""
    bullets: list[str]


class Experience(BaseModel):
    role: str
    org: str
    period: str = ""
    bullets: list[str]


class TailoredCV(BaseModel):
    headline: str  # p. ej. "Desarrollador Full Stack Junior · Python y React"
    summary: str  # 2-3 frases orientadas a la oferta
    skills: list[str]  # de más a menos relevante para la oferta
    projects: list[Project]
    experience: list[Experience] = []
    education: list[str]
    languages: list[str] = []
    gaps: list[str] = []  # lo que pide la oferta y el CV no demuestra (para ti; no va al PDF)


SYSTEM = """Eres un experto en hojas de vida para el mercado tecnológico en Colombia. Adaptas el CV \
de un candidato junior a una oferta concreta.

REGLA DURA: NUNCA inventes experiencia. Usa SOLO hechos que aparecen en el CV de abajo: \
habilidades, proyectos, empresas, fechas, cifras y logros. Puedes reordenar, resumir, elegir \
qué mostrar y redactar mejor, pero no añadir nada que el CV no diga. Si la oferta pide algo \
que el CV no demuestra, NO lo pongas en el CV: anótalo en "gaps".

Cómo adaptar:
- "headline": cargo al que apunta + 2 o 3 tecnologías del CV que pide la oferta.
- "summary": 2 o 3 frases en primera persona implícita (sin "yo"), honestas sobre el nivel junior.
- "skills": solo habilidades del CV, primero las que pide la oferta. Máximo 14, nombres cortos.
- "projects": los 2 a 4 proyectos del CV más relevantes, con el nombre EXACTO del CV. 2 a 4 \
viñetas por proyecto que empiecen con verbo y resalten lo que pide la oferta. No inventes cifras.
- "experience": solo si el CV tiene experiencia laboral o prácticas; si no, lista vacía.
- "education" y "languages": tal como aparecen en el CV (el nivel de inglés exacto).
- Escribe en el idioma de la oferta (español o inglés).
- No incluyas teléfono, correo ni enlaces: se agregan aparte.

<cv>
{cv}
</cv>"""


def user_prompt(job: Job) -> str:
    return f"<oferta>\n{_job_block(job, job.description)}\n</oferta>\n\nAdapta el CV a esta oferta."


# --- Verificación anti-invención ---------------------------------------------------------------

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _in_cv(term: str, cv_norm: str) -> bool:
    """El término (o todas sus palabras significativas) aparece en el CV."""
    t = _norm(term).strip(" .,-·")
    if not t:
        return False
    if t in cv_norm:
        return True
    words = [w for w in re.findall(r"[\w.#+]+", t) if len(w) > 1]
    return bool(words) and all(re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", cv_norm) for w in words)


def _numbers_ok(text: str, cv_norm: str) -> bool:
    """Toda cifra de la viñeta (30 %, 2023, 5…) debe existir en el CV: así no hay métricas inventadas."""
    return all(re.search(rf"(?<![\d.,]){re.escape(n)}(?![\d])", cv_norm)
               for n in re.findall(r"\d+(?:[.,]\d+)?", text))


def verify(cv: TailoredCV, cv_text: str) -> tuple[TailoredCV, list[str]]:
    """Quita lo que no está en el CV maestro. Devuelve el CV limpio y lo que se quitó."""
    cv_norm = _norm(cv_text)
    removed: list[str] = []

    def keep(kind: str, value: str, ok: bool) -> bool:
        if not ok:
            removed.append(f"{kind}: {value}")
        return ok

    skills = []
    for s in cv.skills:
        if keep("habilidad", s, _in_cv(s, cv_norm)) and _norm(s) not in map(_norm, skills):
            skills.append(s)
    projects = []
    for p in cv.projects:
        if not keep("proyecto", p.name, _in_cv(p.name, cv_norm)):
            continue
        bullets = [b for b in p.bullets if keep("cifra", b, _numbers_ok(b, cv_norm))]
        stack = ", ".join(x.strip() for x in p.stack.split(",") if x.strip() and _in_cv(x, cv_norm))
        projects.append(p.model_copy(update={"bullets": bullets, "stack": stack}))
    experience = []
    for e in cv.experience:
        if not keep("experiencia", f"{e.role} en {e.org}", _in_cv(e.org, cv_norm)):
            continue
        bullets = [b for b in e.bullets if keep("cifra", b, _numbers_ok(b, cv_norm))]
        period = e.period if _numbers_ok(e.period, cv_norm) else ""
        experience.append(e.model_copy(update={"bullets": bullets, "period": period}))
    summary = cv.summary if keep("cifra en el resumen", cv.summary, _numbers_ok(cv.summary, cv_norm)) else \
        re.sub(r"[^.]*\d[^.]*\.?\s*", "", cv.summary).strip()
    clean = cv.model_copy(update={"skills": skills[:14], "projects": projects, "experience": experience,
                                  "summary": summary})
    return clean, removed


# --- IA ----------------------------------------------------------------------------------------

def generate(scorer, job: Job) -> TailoredCV:
    """Pide el CV adaptado al mismo proveedor que puntúa (Gemini o Claude)."""
    system = SYSTEM.format(cv=scorer.cv_text)
    try:
        if hasattr(scorer, "fallback_model"):  # GeminiScorer
            return _gemini(scorer, system, user_prompt(job))
        response = scorer.client.beta.messages.parse(
            model=scorer.model, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": user_prompt(job)}], output_format=TailoredCV)
        if response.parsed_output is None:
            raise ScoringError(f"Respuesta incompleta (stop_reason={response.stop_reason})")
        return response.parsed_output
    except Exception as e:
        raise as_scoring_error(e) from e


def _gemini(scorer, system: str, prompt: str) -> TailoredCV:
    from google.genai import errors

    cfg = {"system_instruction": system, "response_mime_type": "application/json",
           "response_json_schema": TailoredCV.model_json_schema()}
    models = [m for m in (scorer.model, scorer.fallback_model) if m]
    for i, model in enumerate(models):
        try:
            response = scorer.client.models.generate_content(model=model, contents=prompt, config=cfg)
        except errors.APIError as e:
            if (e.code or 0) >= 500 and i + 1 < len(models):
                continue  # modelo saturado: probar el de respaldo
            raise
        if not getattr(response, "text", None):
            raise ScoringError("Gemini no devolvió el CV")
        return TailoredCV.model_validate_json(response.text)
    raise ScoringError("Gemini no respondió", transient=True)


# --- Contacto, HTML y PDF ----------------------------------------------------------------------

def load_contact(path: Path = CONTACT_PATH) -> dict[str, str]:
    """`cv/contacto.md` con líneas "Nombre: …", "Correo: …", "Teléfono: …", "LinkedIn: …"…"""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.lstrip("-* ").partition(":")
        if sep and key.strip() and value.strip() and not key.startswith("#"):
            out[key.strip()] = value.strip()
    return out


def render_html(cv: TailoredCV, contact: dict[str, str], job: Job) -> str:
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape())
    english = bool(re.search(r"(?i)\b(the|and|you|with|experience|requirements)\b", job.description or ""))
    name = contact.get("Nombre") or contact.get("Name") or "Tu nombre"
    links = [v for k, v in contact.items() if k not in ("Nombre", "Name")]
    return env.get_template("cv.html").render(cv=cv, name=name, links=links, english=english)


BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str | None:
    if os.getenv("CV_BROWSER"):
        return os.getenv("CV_BROWSER")
    for b in BROWSERS:
        if Path(b).exists():
            return b
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        if found := shutil.which(name):
            return found
    return None


def html_to_pdf(html: str, out: Path, browser: str | None = None) -> Path:
    browser = browser or find_browser()
    if not browser:
        raise RuntimeError("No encontré Edge ni Chrome para crear el PDF (pon la ruta en CV_BROWSER).")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "cv.html"
        page.write_text(html, encoding="utf-8")
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-pdf-header-footer",
                        f"--user-data-dir={Path(tmp) / 'perfil'}", f"--print-to-pdf={out}", str(page)],
                       check=True, capture_output=True, timeout=90)
        # Edge en Windows puede salir antes de terminar de escribir: esperar a que el archivo se estabilice
        if not _wait_for_file(out):
            raise RuntimeError("El navegador no generó el PDF")
    return out


def _wait_for_file(path: Path, timeout: float = 30, clock=time.monotonic, sleep=time.sleep) -> bool:
    end, last = clock() + timeout, -1
    while clock() < end:
        size = path.stat().st_size if path.exists() else -1
        if size > 0 and size == last:
            return True
        last = size
        sleep(0.5)
    return False


def out_path(job: Job, suffix: str = ".pdf") -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", _norm(f"{job.company or ''} {job.title}")).strip("-")[:70] or "oferta"
    return OUT_DIR / f"{slug}{suffix}"


def latest_pdf(job: Job) -> Path | None:
    p = out_path(job)
    return p if p.exists() else None


def build(job: Job, scorer, contact: dict[str, str] | None = None, pdf: bool = True) -> tuple[Path, list[str], list[str]]:
    """Genera el CV adaptado. Devuelve (archivo, lo que se quitó por no estar en el CV, gaps)."""
    if not (job.description or "").strip():
        raise ValueError("La oferta no tiene descripción: pégala primero en el panel")
    tailored, removed = verify(generate(scorer, job), scorer.cv_text)
    html = render_html(tailored, load_contact() if contact is None else contact, job)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path(job, ".json").write_text(tailored.model_dump_json(indent=2), encoding="utf-8")
    if not pdf:
        target = out_path(job, ".html")
        target.write_text(html, encoding="utf-8")
        return target, removed, tailored.gaps
    return html_to_pdf(html, out_path(job)), removed, tailored.gaps


def main() -> None:
    from radar import score
    from radar.db import SessionLocal, init_db

    ap = argparse.ArgumentParser(description="CV adaptado a una oferta, en PDF")
    ap.add_argument("job_id", help='id de la oferta, p. ej. "linkedin:4484196886"')
    ap.add_argument("--html", action="store_true", help="guardar HTML en vez de PDF")
    args = ap.parse_args()
    init_db()
    with SessionLocal() as s:
        job = s.get(Job, args.job_id)
        if job is None:
            sys.exit(f"No existe la oferta {args.job_id}")
        try:
            path, removed, gaps = build(job, score.load_scorer(), pdf=not args.html)
        except (score.ScoreSetupError, ScoringError, ValueError, RuntimeError) as e:
            sys.exit(str(e))
    print(f"CV listo: {path}")
    if removed:
        print("Quitado por no estar en tu CV maestro:\n  " + "\n  ".join(removed))
    if gaps:
        print("La oferta pide y tu CV no demuestra:\n  " + "\n  ".join(gaps))


if __name__ == "__main__":
    main()
