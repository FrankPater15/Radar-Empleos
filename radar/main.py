"""Panel web.  Uso:  uvicorn radar.main:app --reload"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from radar.db import SessionLocal, init_db
from radar.models import STATUSES, Job
from radar.sources import getonbrd, himalayas, jobicy

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

SOURCE_NAMES = {m.SOURCE: m.SOURCE_NAME for m in (getonbrd, himalayas, jobicy)}
ORDERS = {"mejor": "Mejor encaje", "afinidad": "Afinidad", "nota": "Nota IA", "recientes": "Más recientes"}


def hace(dt: datetime | None) -> str:
    """'hace 3 días' para fechas de publicación."""
    if not dt:
        return ""
    days = (datetime.now(timezone.utc) - dt.replace(tzinfo=dt.tzinfo or timezone.utc)).days
    if days <= 0:
        return "hoy"
    if days == 1:
        return "ayer"
    if days < 30:
        return f"hace {days} días"
    return f"hace {days // 30} mes{'es' if days >= 60 else ''}"


templates.env.filters["hace"] = hace
templates.env.globals["SOURCE_NAMES"] = SOURCE_NAMES


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Radar de Empleo CO", lifespan=lifespan)
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
    recent = Job.published_at.desc().nulls_last()
    if order == "afinidad":
        stmt = stmt.order_by(Job.fit.desc().nulls_last(), recent)
    elif order == "recientes":
        stmt = stmt.order_by(recent)
    else:  # "mejor" y "nota": nota de IA primero; sin nota, por afinidad
        stmt = stmt.order_by(Job.score.desc().nulls_last(), Job.fit.desc().nulls_last(), recent)
    stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def distinct_values(session: Session, column) -> list[str]:
    return [v for v in session.scalars(select(column).distinct().order_by(column)) if v]


def stats(session: Session) -> dict:
    def count(*where):
        return session.scalar(select(func.count()).select_from(Job).where(*where))

    co = Job.open_to_colombia.is_(True)
    active = Job.status != "descartada"
    by_status = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    return {
        "total": count(),
        "colombia": count(co),
        "promising": count(co, active, Job.fit >= 60),
        "top": count(co, active, Job.score >= 4),
        "scored": count(Job.score.is_not(None)),
        "by_status": {s: by_status.get(s, 0) for s in STATUSES},
    }


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = "",
    seniority: str = "",
    category: str = "",
    status: str = "",
    colombia: str = "1",
    min_score: str = "",
    order: str = "mejor",
    source: str = "",
    min_fit: str = "",
    limit: int = 60,
    session: Session = Depends(get_session),
):
    ms = int(min_score) if min_score.isdigit() else 0
    mf = int(min_fit) if min_fit.isdigit() else 0
    limit = max(20, min(limit, 1000))
    order = order if order in ORDERS else "mejor"
    jobs = query_jobs(session, q, seniority, category, status, colombia == "1", limit + 1, ms, order,
                      source, mf)
    filters = {"q": q, "seniority": seniority, "category": category, "status": status,
               "colombia": colombia, "min_score": min_score, "order": order, "source": source,
               "min_fit": min_fit}
    more = {k: v for k, v in filters.items() if v} | {"limit": limit + 60}
    active = [k for k in ("q", "seniority", "category", "status", "min_score", "source", "min_fit")
              if filters[k]]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "jobs": jobs[:limit],
            "has_more": len(jobs) > limit,
            "more_query": urlencode(more),
            "filters": filters,
            "active_filters": len(active) + (colombia != "1"),
            "seniorities": distinct_values(session, Job.seniority),
            "categories": distinct_values(session, Job.category),
            "sources": distinct_values(session, Job.source),
            "statuses": STATUSES,
            "orders": ORDERS,
            "stats": stats(session),
        },
    )


@app.get("/api/jobs")
def api_jobs(
    q: str = "", seniority: str = "", category: str = "", status: str = "",
    colombia: bool = True, limit: int = 200, min_score: int = 0, order: str = "mejor",
    source: str = "", min_fit: int = 0,
    session: Session = Depends(get_session),
):
    jobs = query_jobs(session, q, seniority, category, status, colombia, min(limit, 1000), min_score,
                      order, source, min_fit)
    return [
        {
            "id": j.id, "title": j.title, "company": j.company, "url": j.url,
            "category": j.category, "seniority": j.seniority,
            "remote_modality": j.remote_modality, "countries": j.countries,
            "min_salary": j.min_salary, "max_salary": j.max_salary,
            "salary_currency": j.salary_currency,
            "published_at": j.published_at.isoformat() if j.published_at else None,
            "status": j.status,
            "source": j.source,
            "fit": j.fit,
            "fit_reasons": j.fit_reasons,
            "score": j.score,
            "score_data": j.score_data,
        }
        for j in jobs
    ]


@app.post("/jobs/{job_id:path}/status")
def set_status(
    job_id: str,
    status: str = Form(...),
    back: str = Form("/"),
    session: Session = Depends(get_session),
):
    if status not in STATUSES:
        raise HTTPException(400, f"Estado inválido: {status}")
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Oferta no encontrada")
    job.status = status
    session.commit()
    # Solo rutas locales, para no redirigir a otros sitios
    if not back.startswith("/") or back.startswith("//") or "\\" in back:
        back = "/"
    return RedirectResponse(back, status_code=303)
