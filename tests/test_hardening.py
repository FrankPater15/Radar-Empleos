"""Endurecimiento: CSV injection, defensas del panel, URLs, ingesta robusta, índices, caché y scoring."""
import csv
import io
from datetime import datetime, timezone

from radar.models import Job
from radar.tracking import export_csv

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


# ---------- 1. CSV injection ----------
def test_export_csv_neutraliza_formulas():
    peligrosos = ['=HYPERLINK("http://evil","x")', "+cmd|' /C calc'!A0", "-2+3", "@SUM(A1)", "\tTAB", "\rCR"]
    jobs = [Job(id=f"a:{i}", source="a", title=t, company=t, url="https://x", status="encontrada",
                notes=t, first_seen_at=NOW) for i, t in enumerate(peligrosos)]
    buf = io.StringIO()
    export_csv(jobs, buf)
    rows = list(csv.reader(io.StringIO(buf.getvalue()), delimiter=";"))[1:]
    assert len(rows) == len(peligrosos)
    for row, original in zip(rows, peligrosos):
        for idx in (1, 2, 12):  # titulo, empresa, notas
            assert row[idx] == "'" + original
    # el texto normal no se toca
    ok = Job(id="a:ok", source="a", title="Analista QA", company="ACME", url="https://x", first_seen_at=NOW)
    buf = io.StringIO()
    export_csv([ok], buf)
    assert list(csv.reader(io.StringIO(buf.getvalue()), delimiter=";"))[1][1] == "Analista QA"


# ---------- 2. Panel: Host, CSRF y cabeceras ----------
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from radar import main  # noqa: E402
from radar.db import init_db  # noqa: E402
from radar.ingest import upsert_jobs  # noqa: E402
from tests.test_search import job  # noqa: E402

LOCAL = "http://127.0.0.1:8000"


@pytest.fixture
def Session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(Session):
    def override():
        with Session() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = override
    with Session() as s:
        upsert_jobs(s, [job(id="a:1", source="a", title="Analista QA", company="A", open_to_colombia=True)])
    yield TestClient(main.app, base_url=LOCAL)
    main.app.dependency_overrides.clear()


def _post(c, **headers):
    return c.post("/jobs/a:1/status", data={"status": "aprobada"}, headers=headers, follow_redirects=False)


def test_host_ajeno_rechazado(client):
    assert client.get("/", headers={"Host": "evil.example.com"}).status_code == 400
    assert client.get("/api/jobs", headers={"Host": "evil.example.com:8000"}).status_code == 400
    assert client.get("/", headers={"Host": "localhost:8000"}).status_code == 200
    assert client.get("/").status_code == 200


def test_post_mismo_origen_ok(client):
    assert _post(client, Origin=LOCAL, **{"Sec-Fetch-Site": "same-origin"}).status_code == 303
    r = client.post("/jobs/a:1/status", data={"status": "enviada"},
                    headers={"Accept": "application/json", "Origin": LOCAL, "Referer": LOCAL + "/?q=a"})
    assert r.status_code == 200 and r.json()["job"]["status"] == "enviada"
    assert _post(client, **{"Sec-Fetch-Site": "none"}).status_code == 303
    assert _post(client).status_code == 303  # curl / tests locales: sin Origin ni Sec-Fetch-Site
    assert client.post("/jobs/a:1/notes", data={"notes": "x"}, headers={"Origin": "http://localhost:8000"},
                       follow_redirects=False).status_code == 403  # otro host que el de la petición


def test_post_origen_ajeno_403(client, Session):
    for headers in ({"Origin": "http://evil.example.com"}, {"Origin": "null"},
                    {"Origin": "http://127.0.0.1:9999"}, {"Origin": "http://127.0.0.1.evil.com:8000"},
                    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                    {"Referer": "http://evil.example.com/x"},
                    {"Origin": LOCAL, "Sec-Fetch-Site": "cross-site"}):
        assert _post(client, **headers).status_code == 403, headers
    for path, data in (("/jobs/a:1/notes", {"notes": "hack"}), ("/jobs/a:1/follow-up", {"action": "hecho"})):
        assert client.post(path, data=data, headers={"Origin": "http://evil.example.com"}).status_code == 403
    with Session() as s:
        j = s.get(Job, "a:1")
        assert j.status == "encontrada" and j.notes is None
    # las lecturas no se bloquean por Sec-Fetch-Site (navegación normal)
    assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_cabeceras_de_seguridad(client):
    for path in ("/", "/api/jobs", "/static/keys.js", "/export.csv"):
        h = client.get(path).headers
        assert h["x-frame-options"] == "DENY", path
        assert h["referrer-policy"] == "no-referrer"
        assert h["x-content-type-options"] == "nosniff"
    assert _post(client, Origin="http://evil.example.com").headers["x-frame-options"] == "DENY"


# ---------- 3. URLs de oferta ----------
def test_safe_url():
    from radar.sources.common import safe_url

    assert safe_url("https://a.com/x?y=1") == "https://a.com/x?y=1"
    assert safe_url("  HTTP://a.com ") == "HTTP://a.com"
    for bad in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>", "//evil.com",
                "ftp://a.com", "", None, 5, "https://", "java\nscript:alert(1)", "https://a.com/ x"):
        assert safe_url(bad) is None, bad


def test_ingesta_descarta_urls_no_http(Session):
    with Session() as s:
        n = upsert_jobs(s, [job(id="a:ok", title="Analista QA", url=" https://a.com/1 "),
                            job(id="a:js", title="Otra", company="B", url="javascript:alert(1)"),
                            job(id="a:data", title="Tercera", company="C", url="data:text/html,x")])
        assert n == (1, 0)
        assert [j.url for j in s.query(Job).all()] == ["https://a.com/1"]


def test_plantilla_no_enlaza_urls_peligrosas(client, Session):
    with Session() as s:  # una fila vieja que ya tenía una URL mala en la BD
        s.add(Job(id="a:mala", source="a", external_id="mala", title="Oferta mala", url="javascript:alert(1)",
                  open_to_colombia=True, countries=[]))
        s.commit()
    html = client.get("/").text
    assert "Oferta mala" in html and "javascript:" not in html
    assert 'href="https://example.com"' in html and 'rel="noopener noreferrer"' in html
    assert 'rel="noopener"' not in html


def test_keys_js_valida_esquema():
    js = client_static("keys.js")
    assert "https?:" in js and "noreferrer" in js


def client_static(name):
    from pathlib import Path

    return (Path(main.HERE) / "static" / name).read_text(encoding="utf-8")
