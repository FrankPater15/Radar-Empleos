"""Panel web.  Uso:  uvicorn radar.main:app --reload"""
from __future__ import annotations

import io
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from radar import config
from radar.db import SessionLocal, init_db
from radar.models import STATUSES, Job
from radar.relevance import extract_requirements, rank_value
from radar.sources import ats, getonbrd, himalayas, jobicy, linkedin_mail
from radar.sources.common import safe_url
from radar.tracking import (apply_status, count_follow_ups, count_new, export_csv, follow_up,
                            follow_up_due_clause, new_since)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

SOURCE_NAMES = {m.SOURCE: m.SOURCE_NAME for m in (getonbrd, himalayas, jobicy, ats, linkedin_mail)}
SOURCE_NAMES[ats.REMOTEOK_SOURCE] = ats.REMOTEOK_NAME
ORDERS = {"mejor": "Mejor encaje", "afinidad": "Afinidad", "nota": "Nota IA", "recientes": "Más recientes"}
COLOMBIA_TZ = timezone(timedelta(hours=-5), "COT")  # Colombia no tiene horario de verano
NOTES_MAX = 5000


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite devuelve UTC sin zona


def hace(dt: datetime | None) -> str:
    """'hace 3 días' para fechas de publicación."""
    if not dt:
        return ""
    days = (datetime.now(timezone.utc) - _aware(dt)).days
    if days <= 0:
        return "hoy"
    if days == 1:
        return "ayer"
    if days < 30:
        return f"hace {days} días"
    return f"hace {days // 30} mes{'es' if days >= 60 else ''}"


def fecha(dt: datetime | None) -> str:
    """'23/11' (hora de Colombia)."""
    return _aware(dt).astimezone(COLOMBIA_TZ).strftime("%d/%m") if dt else ""


def vencido(dt: datetime | None) -> bool:
    return bool(dt) and _aware(dt) <= datetime.now(timezone.utc)


def es_nueva(dt: datetime | None, since: datetime | None) -> bool:
    return bool(dt and since) and _aware(dt) >= _aware(since)


templates.env.filters["hace"] = hace
templates.env.filters["fecha"] = fecha
templates.env.tests["vencido"] = vencido
templates.env.globals["SOURCE_NAMES"] = SOURCE_NAMES
templates.env.globals["es_nueva"] = es_nueva
templates.env.globals["requisitos"] = extract_requirements
templates.env.globals["safe_url"] = safe_url
templates.env.globals["google_fonts"] = config.GOOGLE_FONTS


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Radar de Empleo CO", lifespan=lifespan)

# El panel no tiene login: solo escucha en local y rechaza peticiones que no vengan de él mismo.
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _foreign_origin(value: str, host: str) -> bool:
    """True si un Origin/Referer no apunta al propio panel (mismo host:puerto y host permitido)."""
    parts = urlsplit(value)
    return parts.scheme not in ("http", "https") or parts.hostname not in ALLOWED_HOSTS or parts.netloc != host


def _is_cross_site(request: Request) -> bool:
    """CSRF: un POST con Origin/Referer ajeno o Sec-Fetch-Site distinto de same-origin/none se rechaza.
    Sin esas cabeceras (curl, tests locales) se acepta: el Host ya lo validó TrustedHostMiddleware."""
    h = request.headers
    if h.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return True
    host = h.get("host", "")
    if "origin" in h:
        return _foreign_origin(h["origin"], host)
    return "referer" in h and _foreign_origin(h["referer"], host)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if request.method not in SAFE_METHODS and _is_cross_site(request):
        response = JSONResponse({"detail": "Origen no permitido"}, status_code=403)
    else:
        response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)  # el último en añadirse va primero
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def get_session():
    with SessionLocal() as session:
        yield session


def query_jobs(
    session: Session,
    q: str = "",
    seniority: str = "",
    category: str = "",
    status: str = "",
    colombia: bool = True,
    limit: int = 200,
    min_score: int = 0,
    order: str = "mejor",
    source: str = "",
    min_fit: int = 0,
    since: datetime | None = None,
    follow_up_due: bool = False,
) -> list[Job]:
    stmt = select(Job)
    if source:
        stmt = stmt.where(Job.source == source)
    if min_fit:
        stmt = stmt.where(Job.fit >= min_fit)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(func.lower(Job.title).like(like), func.lower(Job.company).like(like),
                func.lower(Job.description).like(like))
        )
    if seniority:
        stmt = stmt.where(Job.seniority == seniority)
    if category:
        stmt = stmt.where(Job.category == category)
    if status:
        stmt = stmt.where(Job.status == status)
    else:
        stmt = stmt.where(Job.status != "descartada")
    if colombia:
        stmt = stmt.where(Job.open_to_colombia.is_(True))
    if min_score:
        stmt = stmt.where(Job.score >= min_score)
    if since is not None:
        stmt = stmt.where(Job.first_seen_at >= _aware(since).astimezone(timezone.utc).replace(tzinfo=None))
    if follow_up_due:
        stmt = stmt.where(follow_up_due_clause())
    recent = Job.published_at.desc().nulls_last()
    if follow_up_due:  # el seguimiento más atrasado primero
        stmt = stmt.order_by(Job.follow_up_at.asc(), recent)
    elif order == "afinidad":
        stmt = stmt.order_by(Job.fit.desc().nulls_last(), recent)
    elif order == "recientes":
        stmt = stmt.order_by(recent)
    elif order == "mejor":  # valor combinado: IA o prefiltro, bloqueadores y antigüedad
        rows = list(session.scalars(stmt))
        rows.sort(key=rank_value, reverse=True)  # estable: empates conservan el orden de la BD
        return rows[:limit]
    else:  # "nota": nota de IA primero; sin nota, por afinidad
        stmt = stmt.order_by(Job.score.desc().nulls_last(), Job.fit.desc().nulls_last(), recent)
    stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def distinct_values(session: Session, column) -> list[str]:
    return [v for v in session.scalars(select(column).distinct().order_by(column)) if v]


def stats(session: Session, since: datetime | None = None) -> dict:
    def count(*where):
        return session.scalar(select(func.count()).select_from(Job).where(*where))

    co = Job.open_to_colombia.is_(True)
    active = Job.status != "descartada"
    by_status = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    since = since or new_since(session)
    return {
        "total": count(),
        "colombia": count(co),
        "promising": count(co, active, Job.fit >= 60),
        "top": count(co, active, Job.score >= 4),
        "scored": count(Job.score.is_not(None)),
        "new": count_new(session, since),
        "follow_ups": count_follow_ups(session),
        "by_status": {s: by_status.get(s, 0) for s in STATUSES},
    }


@dataclass
class Filters:
    """Filtros del panel (los mismos sirven para /export.csv)."""

    q: str = ""
    seniority: str = ""
    category: str = ""
    status: str = ""
    colombia: str = "1"
    min_score: str = ""
    order: str = "mejor"
    source: str = ""
    min_fit: str = ""
    nuevas: str = ""
    seguimiento: str = ""

    ACTIVE = ("q", "seniority", "category", "status", "min_score", "source", "min_fit", "nuevas",
              "seguimiento")

    def __post_init__(self):
        self.order = self.order if self.order in ORDERS else "mejor"
        self.nuevas = "1" if self.nuevas == "1" else ""
        self.seguimiento = "1" if self.seguimiento == "1" else ""

    def active_count(self) -> int:
        return sum(1 for k in self.ACTIVE if getattr(self, k)) + (self.colombia != "1")

    def query_args(self, session: Session) -> dict:
        return {
            "q": self.q, "seniority": self.seniority, "category": self.category, "status": self.status,
            "colombia": self.colombia == "1",
            "min_score": int(self.min_score) if self.min_score.isdigit() else 0,
            "order": self.order, "source": self.source,
            "min_fit": int(self.min_fit) if self.min_fit.isdigit() else 0,
            "since": new_since(session) if self.nuevas else None,
            "follow_up_due": bool(self.seguimiento),
        }

    def params(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v and not (k == "order" and v == "mejor")}


def get_filters(q: str = "", seniority: str = "", category: str = "", status: str = "",
                colombia: str = "1", min_score: str = "", order: str = "mejor", source: str = "",
                min_fit: str = "", nuevas: str = "", seguimiento: str = "") -> Filters:
    return Filters(q, seniority, category, status, colombia, min_score, order, source, min_fit, nuevas,
                   seguimiento)


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    filters: Filters = Depends(get_filters),
    limit: int = 60,
    session: Session = Depends(get_session),
):
    limit = max(20, min(limit, 1000))
    jobs = query_jobs(session, **(filters.query_args(session) | {"limit": limit + 1}))
    since = new_since(session)
    export_query = urlencode(filters.params())
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "jobs": jobs[:limit],
            "has_more": len(jobs) > limit,
            "more_query": urlencode(filters.params() | {"limit": limit + 60}),
            "export_query": export_query,
            "filters": filters,
            "active_filters": filters.active_count(),
            "new_since": since,
            "seniorities": distinct_values(session, Job.seniority),
            "categories": distinct_values(session, Job.category),
            "sources": distinct_values(session, Job.source),
            "statuses": STATUSES,
            "orders": ORDERS,
            "stats": stats(session, since),
        },
    )


@app.get("/export.csv")
def export(filters: Filters = Depends(get_filters), session: Session = Depends(get_session)):
    """Las ofertas que ves en el panel (mismos filtros, sin límite de página) en CSV para Excel."""
    jobs = query_jobs(session, **(filters.query_args(session) | {"limit": 100_000}))
    buf = io.StringIO()
    export_csv(jobs, buf, SOURCE_NAMES)
    name = f"radar-empleo-{datetime.now(COLOMBIA_TZ):%Y-%m-%d}.csv"
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/jobs")
def api_jobs(
    q: str = "", seniority: str = "", category: str = "", status: str = "",
    colombia: bool = True, limit: int = 200, min_score: int = 0, order: str = "mejor",
    source: str = "", min_fit: int = 0,
    session: Session = Depends(get_session),
):
    jobs = query_jobs(session, q, seniority, category, status, colombia, min(limit, 1000), min_score,
                      order, source, min_fit)
    return [job_json(j) for j in jobs]


def _iso(dt: datetime | None) -> str | None:
    return _aware(dt).isoformat() if dt else None


def job_json(j: Job) -> dict:
    return {
        "id": j.id, "title": j.title, "company": j.company, "url": j.url,
        "category": j.category, "seniority": j.seniority,
        "remote_modality": j.remote_modality, "countries": j.countries,
        "min_salary": j.min_salary, "max_salary": j.max_salary,
        "salary_currency": j.salary_currency,
        "published_at": _iso(j.published_at),
        "status": j.status,
        "source": j.source,
        "fit": j.fit,
        "fit_reasons": j.fit_reasons,
        "score": j.score,
        "score_data": j.score_data,
        "notes": j.notes,
        "applied_at": _iso(j.applied_at),
        "follow_up_at": _iso(j.follow_up_at),
    }


def safe_back(back: str) -> str:
    """Solo rutas locales, para no redirigir a otros sitios."""
    if not back.startswith("/") or back.startswith("//") or "\\" in back:
        return "/"
    return back


def wants_json(request: Request) -> bool:
    """El JS del panel pide JSON (Accept: application/json); sin JS, el formulario redirige."""
    return "application/json" in request.headers.get("accept", "")


def counts(session: Session) -> dict:
    by_status = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    return {"by_status": {s: by_status.get(s, 0) for s in STATUSES},
            "follow_ups": count_follow_ups(session)}


def get_job(session: Session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Oferta no encontrada")
    return job


@app.post("/jobs/{job_id:path}/status")
def set_status(
    request: Request,
    job_id: str,
    status: str = Form(...),
    back: str = Form("/"),
    session: Session = Depends(get_session),
):
    if status not in STATUSES:
        raise HTTPException(400, f"Estado inválido: {status}")
    job = get_job(session, job_id)
    previous = job.status
    apply_status(job, status)
    session.commit()
    if wants_json(request):
        return JSONResponse({"job": job_json(job), "previous": previous, "counts": counts(session),
                             "follow_up": fecha(job.follow_up_at) if job.status == "enviada" else ""})
    return RedirectResponse(safe_back(back), status_code=303)


@app.post("/jobs/{job_id:path}/notes")
def set_notes(
    request: Request,
    job_id: str,
    notes: str = Form(""),
    back: str = Form("/"),
    session: Session = Depends(get_session),
):
    job = get_job(session, job_id)
    notes = notes.strip()
    if len(notes) > NOTES_MAX:
        raise HTTPException(400, f"Las notas no pueden pasar de {NOTES_MAX} caracteres")
    job.notes = notes or None
    session.commit()
    if wants_json(request):
        return JSONResponse({"job": job_json(job)})
    return RedirectResponse(safe_back(back), status_code=303)


@app.post("/jobs/{job_id:path}/follow-up")
def set_follow_up(
    job_id: str,
    action: str = Form(...),
    back: str = Form("/"),
    session: Session = Depends(get_session),
):
    if action not in ("hecho", "posponer"):
        raise HTTPException(400, f"Acción inválida: {action}")
    follow_up(get_job(session, job_id), action)
    session.commit()
    return RedirectResponse(safe_back(back), status_code=303)
