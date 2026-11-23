"""Panel web.  Uso:  uvicorn radar.main:app --reload"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from radar.db import SessionLocal, init_db
from radar.models import STATUSES, Job

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


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
) -> list[Job]:
    stmt = select(Job)
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
    stmt = stmt.order_by(Job.published_at.desc().nulls_last()).limit(limit)
    return list(session.scalars(stmt))


def distinct_values(session: Session, column) -> list[str]:
    return [v for v in session.scalars(select(column).distinct().order_by(column)) if v]


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = "",
    seniority: str = "",
    category: str = "",
    status: str = "",
    colombia: str = "1",
    session: Session = Depends(get_session),
):
    jobs = query_jobs(session, q, seniority, category, status, colombia == "1")
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "jobs": jobs,
            "filters": {"q": q, "seniority": seniority, "category": category,
                        "status": status, "colombia": colombia},
            "seniorities": distinct_values(session, Job.seniority),
            "categories": distinct_values(session, Job.category),
            "statuses": STATUSES,
            "total": session.scalar(select(func.count()).select_from(Job)),
        },
    )


@app.get("/api/jobs")
def api_jobs(
    q: str = "", seniority: str = "", category: str = "", status: str = "",
    colombia: bool = True, limit: int = 200, session: Session = Depends(get_session),
):
    jobs = query_jobs(session, q, seniority, category, status, colombia, min(limit, 1000))
    return [
        {
            "id": j.id, "title": j.title, "company": j.company, "url": j.url,
            "category": j.category, "seniority": j.seniority,
            "remote_modality": j.remote_modality, "countries": j.countries,
            "min_salary": j.min_salary, "max_salary": j.max_salary,
            "salary_currency": j.salary_currency,
            "published_at": j.published_at.isoformat() if j.published_at else None,
            "status": j.status,
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
    if not back.startswith("/") or back.startswith("//"):
        back = "/"
    return RedirectResponse(back, status_code=303)
