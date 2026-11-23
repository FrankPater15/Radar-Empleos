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


# ---------- 4. La ingesta no retiene el bloqueo de escritura durante la descarga ----------
import sqlite3  # noqa: E402
from contextlib import nullcontext  # noqa: E402

from radar import ingest  # noqa: E402
from radar.db import make_engine  # noqa: E402


def test_ingesta_no_bloquea_la_bd_mientras_descarga(tmp_path, monkeypatch):
    path = tmp_path / "lock.db"
    engine = make_engine(f"sqlite:///{path.as_posix()}")
    init_db(engine)
    FileSession = sessionmaker(bind=engine, expire_on_commit=False)
    with FileSession() as s:
        upsert_jobs(s, [job(id="a:u", source="a", title="Oferta del usuario", company="U")])
    writes = []

    def lenta():
        yield job(id="b:1", source="b", title="Soporte TI", company="B")
        # "el usuario" guarda una nota desde el panel justo en mitad de la descarga
        other = sqlite3.connect(path, timeout=0.2)
        try:
            other.execute("UPDATE jobs SET notes = 'nota' WHERE id = 'a:u'")
            other.commit()
            writes.append("ok")
        finally:
            other.close()
        yield job(id="b:2", source="b", title="Otra oferta QA", company="C")

    monkeypatch.setattr(ingest, "source_streams", lambda sources, client, log: iter([("b", lenta())]))
    results = ingest.run(["b"], log=lambda m: None, session_factory=FileSession, client_factory=nullcontext)
    assert writes == ["ok"] and results == {"b": (2, 0)}
    engine.dispose()


# ---------- 5. Un ítem corrupto no aborta la fuente ----------
import json  # noqa: E402
import logging  # noqa: E402
from pathlib import Path  # noqa: E402

from radar.sources import ats, getonbrd, himalayas, jobicy  # noqa: E402
from tests.test_search import HIMALAYAS_JOB, JOBICY_JOB  # noqa: E402


def test_getonbrd_item_corrupto(monkeypatch, caplog):
    page = json.loads((Path(__file__).parent / "fixtures" / "getonbrd_page.json").read_text(encoding="utf-8"))
    good = page["data"]
    bad = [{"id": "x"}, {"id": "y", "attributes": {"seniority": {"data": {"id": "abc"}}}},
           {"id": "z", "attributes": {"published_at": 10**18}}, "no soy un dict", None]
    page["data"] = [bad[0], good[0], bad[1], bad[2], good[1], bad[3], bad[4], good[2]]
    monkeypatch.setattr(getonbrd, "get_json", lambda *a, **k: page)
    with caplog.at_level(logging.WARNING, logger="radar.sources"):
        jobs = list(getonbrd.fetch_category("programming", max_pages=1, delay_s=0))
    assert [j["external_id"] for j in jobs] == [g["id"] for g in good]
    assert "omitida" in caplog.text
    # payload que no es dict: se corta esa página sin excepción
    monkeypatch.setattr(getonbrd, "get_json", lambda *a, **k: [1, 2])
    assert list(getonbrd.fetch_category("programming", max_pages=1, delay_s=0)) == []


def test_himalayas_item_corrupto(monkeypatch):
    bad = {**HIMALAYAS_JOB, "parentCategories": [5], "locationRestrictions": "Colombia", "pubDate": "x" * 30}
    worse = {**HIMALAYAS_JOB, "pubDate": 10**20}
    payload = {"jobs": [worse, HIMALAYAS_JOB, "basura", bad], "totalCount": 4, "offset": 0}
    monkeypatch.setattr(himalayas, "get_json", lambda *a, **k: payload)
    jobs = list(himalayas.search("qa", max_pages=1))
    assert HIMALAYAS_JOB["guid"] in [j["url"] for j in jobs] and len(jobs) >= 1
    monkeypatch.setattr(himalayas, "get_json", lambda *a, **k: ["no es dict"])
    assert list(himalayas.search("qa", max_pages=1)) == []


def test_jobicy_item_corrupto(monkeypatch):
    payload = {"jobs": [{"jobTitle": "sin id"}, JOBICY_JOB, {**JOBICY_JOB, "id": 2, "pubDate": 12345}, 7]}
    monkeypatch.setattr(jobicy, "get_json", lambda *a, **k: payload)
    jobs = list(jobicy.fetch_all(["dev"], client=object(), delay_s=0))
    assert [j["external_id"] for j in jobs] == ["163890", "2"]


def test_ats_item_corrupto(monkeypatch):
    co = {"name": "Acme", "ats": "greenhouse", "token": "acme"}
    payload = {"jobs": [{"title": "sin id"}, None,
                        {"id": 1, "title": "Dev", "absolute_url": "https://x/1", "location": {"name": "Colombia"}},
                        {"id": 2, "title": "Dev 2", "absolute_url": "https://x/2", "location": "mal"}]}
    monkeypatch.setattr(ats, "get_json", lambda *a, **k: payload)
    ids = [j["external_id"] for j in ats.fetch_greenhouse(None, co)]
    assert ids == ["greenhouse:acme:1"]
    assert [j["external_id"] for j in ats.fetch_company(None, co)] == ["greenhouse:acme:1"]


# ---------- 6. Migración: índices que faltan ----------
def test_init_db_crea_indices_faltantes(tmp_path):
    from sqlalchemy import inspect, text

    engine = make_engine(f"sqlite:///{(tmp_path / 'vieja.db').as_posix()}")
    init_db(engine)
    esperados = {"ix_jobs_fit", "ix_jobs_score", "ix_jobs_dedupe_key", "ix_jobs_follow_up_at"}
    nombres = {i["name"] for i in inspect(engine).get_indexes("jobs")}
    assert esperados <= nombres
    with engine.begin() as c:  # simula una BD antigua sin esos índices
        for name in esperados:
            c.execute(text(f"DROP INDEX {name}"))
    assert not esperados & {i["name"] for i in inspect(engine).get_indexes("jobs")}
    init_db(engine)
    init_db(engine)  # idempotente
    assert esperados <= {i["name"] for i in inspect(engine).get_indexes("jobs")}
    engine.dispose()


def test_init_db_columna_duplicada_no_tumba_el_arranque(tmp_path, monkeypatch):
    from sqlalchemy import inspect, text

    engine = make_engine(f"sqlite:///{(tmp_path / 'carrera.db').as_posix()}")
    init_db(engine)
    with engine.begin() as c:
        c.execute(text("DROP INDEX ix_jobs_follow_up_at"))
        c.execute(text("ALTER TABLE jobs DROP COLUMN follow_up_at"))
    real = inspect

    class Stale:  # el inspector aún no ve la columna que otro proceso acaba de añadir
        def __init__(self, bind):
            self.insp = real(bind)

        def get_columns(self, table):
            return [c for c in self.insp.get_columns(table) if c["name"] != "applied_at"]

    monkeypatch.setattr("radar.db.inspect", Stale)
    init_db(engine)  # ALTER ADD COLUMN applied_at -> "duplicate column name": se ignora
    engine.dispose()


# ---------- 7. Caché de extract_requirements ----------
def test_extract_requirements_usa_cache_y_se_invalida(monkeypatch):
    from radar import relevance

    relevance._REQ_CACHE.clear()
    llamadas = []
    real = relevance._extract_requirements
    monkeypatch.setattr(relevance, "_extract_requirements", lambda j: llamadas.append(1) or real(j))
    j = job(id="c:1", title="Dev QA", description="Requiere 4 años de experiencia. Inglés fluido.")
    a = relevance.extract_requirements(j)
    b = relevance.extract_requirements(j)
    assert len(llamadas) == 1 and a == b and a["anos_min"] == 4
    a["bloqueadores"].append("mutado")  # modificar el resultado no contamina la caché
    assert "mutado" not in relevance.extract_requirements(j)["bloqueadores"]
    # un Job de otra petición (otro objeto, mismo contenido) también acierta
    relevance.extract_requirements(Job(**{k: v for k, v in j.items() if k in Job.__table__.columns.keys()}))
    assert len(llamadas) == 1
    # cambiar la descripción, o la config de la que depende, recalcula
    j2 = {**j, "description": "Requiere 1 año de experiencia."}
    assert relevance.extract_requirements(j2)["anos_min"] != 4 and len(llamadas) == 2
    monkeypatch.setattr(relevance.config, "ENGLISH_LEVEL", "C1")
    assert not any("inglés" in x for x in relevance.extract_requirements(j)["bloqueadores"])
    assert len(llamadas) == 3
