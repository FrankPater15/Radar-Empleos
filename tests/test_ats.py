import json

import httpx
import pytest

from radar.sources import ats

CO = {"name": "Acme", "ats": "x", "token": "acme"}


def test_geo_rules():
    g = ats.geo_ats
    assert g(["Bogotá, Colombia"])
    assert g(["Remote - LATAM"], remote=True)
    assert g(["Anywhere"], remote=True)
    assert not g(["Anywhere"], remote=False)
    assert g(["Remote"], country="CO")
    assert not g(["Remote - US"], remote=True)
    assert not g(["Remote"], remote=True)            # sin país: ambiguo
    assert not g(["Home Based - Americas"])           # 'Americas' a secas se descarta
    assert g(["Americas", "Colombia"])
    assert not g([None, ""])


def test_greenhouse_parse_and_geo():
    j = {"id": 7, "title": "Junior Developer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/7",
         "location": {"name": "Bogotá, Colombia"}, "offices": [{"name": "Austin"}],
         "first_published": "2026-11-01T10:00:00-05:00", "departments": [{"name": "Eng"}],
         "content": "&lt;p&gt;Hola &amp;amp; bienvenido&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"}
    d = ats.parse_greenhouse(j, CO)
    assert d["id"] == "ats:greenhouse:acme:7" and d["source"] == "ats"
    assert d["source_name"] == "Acme (ATS)" and d["url"].endswith("/jobs/7")
    assert d["open_to_colombia"] and d["seniority"] == "Junior"
    assert "Python" in d["description"] and "<" not in d["description"]
    assert d["published_at"].year == 2026
    us = ats.parse_greenhouse({**j, "location": {"name": "Remote - US"}, "offices": [{"name": "Bogota"}]}, CO)
    assert not us["open_to_colombia"]  # las oficinas ajenas no cuentan si hay ubicación propia


def test_lever_parse_and_geo():
    j = {"id": "abc", "text": "Senior QA", "hostedUrl": "https://jobs.lever.co/acme/abc", "createdAt": 1762000000000,
         "categories": {"location": "Remote", "allLocations": ["Remote"], "team": "QA"}, "country": "CO",
         "workplaceType": "remote", "descriptionPlain": "Descripción", "lists": [{"text": "Req", "content": "<li>SQL</li>"}]}
    d = ats.parse_lever(j, CO)
    assert d["open_to_colombia"] and d["remote_modality"] == "fully_remote" and d["seniority"] == "Senior"
    assert "SQL" in d["description"] and d["published_at"].year == 2025
    assert not ats.parse_lever({**j, "country": "US"}, CO)["open_to_colombia"]
    assert ats.parse_lever({**j, "country": "US", "categories": {"location": "Medellín"}}, CO)["open_to_colombia"]


def test_ashby_parse_and_geo():
    j = {"id": "u1", "title": "Backend Engineer", "location": "Remote", "isRemote": True,
         "secondaryLocations": [{"location": "LATAM"}], "jobUrl": "https://jobs.ashbyhq.com/acme/u1",
         "descriptionHtml": "<p>Texto</p>", "publishedAt": "2026-11-02T00:00:00.000+00:00", "workplaceType": "Remote"}
    d = ats.parse_ashby(j, CO)
    assert d["open_to_colombia"] and d["description"] == "Texto" and d["remote_modality"] == "fully_remote"
    assert not ats.parse_ashby({**j, "secondaryLocations": [], "location": "United States"}, CO)["open_to_colombia"]
    co = ats.parse_ashby({**j, "secondaryLocations": [], "location": "Bogotá", "isRemote": False,
                          "address": {"postalAddress": {"addressCountry": "Colombia"}}}, CO)
    assert co["open_to_colombia"] and co["remote_modality"] == "no_remote"


def test_smartrecruiters_parse():
    j = {"id": "123", "name": "Dev", "releasedDate": "2026-11-03T12:00:00.000Z",
         "location": {"city": "Medellín", "country": "co", "remote": False, "hybrid": True, "fullLocation": "Medellín, Colombia"},
         "experienceLevel": {"id": "entry_level"}, "department": {"label": "IT"}}
    detail = {"postingUrl": "https://jobs.smartrecruiters.com/acme/123-dev",
              "jobAd": {"sections": {"jobDescription": {"text": "<p>Haz cosas</p>"}}}}
    d = ats.parse_smartrecruiters(j, CO, detail)
    assert d["seniority"] == "Junior" and d["remote_modality"] == "hybrid" and d["open_to_colombia"]
    assert d["url"].endswith("123-dev") and d["description"] == "Haz cosas"
    assert ats.parse_smartrecruiters(j, CO)["url"] == "https://jobs.smartrecruiters.com/acme/123"


def test_workable_parse():
    j = {"title": "QA Analyst", "shortcode": "ABC123", "url": "https://apply.workable.com/acme/j/ABC123/",
         "country": "Colombia", "city": "Medellín", "telecommuting": False, "published_on": "2026-11-05",
         "description": "<p>QA</p>"}
    d = ats.parse_workable(j, CO)
    assert d["open_to_colombia"] and d["id"] == "ats:workable:acme:ABC123" and d["description"] == "QA"
    assert not ats.parse_workable({**j, "country": "United States", "city": "Austin", "telecommuting": True}, CO)["open_to_colombia"]


def test_bamboohr_parse():
    j = {"id": "5", "jobOpeningName": "Dev Jr", "departmentLabel": "Eng", "isRemote": False,
         "location": {"city": "Medellín", "state": "Antioquia"}, "atsLocation": {"country": "Colombia"}}
    d = ats.parse_bamboohr(j, CO, {"result": {"jobOpening": {"description": "<p>Hola</p>", "datePosted": "2026-11-01"}}})
    assert d["open_to_colombia"] and d["url"] == "https://acme.bamboohr.com/careers/5" and d["description"] == "Hola"
    other = ats.parse_bamboohr({**j, "location": {"city": "Austin", "state": "TX"}, "atsLocation": {"country": "United States"}}, CO)
    assert not other["open_to_colombia"]


def test_remoteok_parse():
    base = {"id": "9", "position": "Python Developer", "company": "X", "url": "https://remoteok.com/remote-jobs/9",
            "epoch": 1762000000, "description": "<p>hi</p>", "tags": ["python"]}
    assert ats.parse_remoteok({**base, "location": ""})["open_to_colombia"]
    assert ats.parse_remoteok({**base, "location": "Worldwide"})["open_to_colombia"]
    assert not ats.parse_remoteok({**base, "location": "United States"})["open_to_colombia"]
    assert ats.parse_remoteok(base)["url"].startswith("https://remoteok.com/")


def test_description_is_clipped():
    d = ats.parse_workable({"title": "x", "shortcode": "1", "url": "u", "country": "Colombia",
                            "description": "a" * 50000}, CO)
    assert len(d["description"]) == ats.MAX_DESCRIPTION


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_all_isolates_failures_and_filters(monkeypatch):
    def handler(req: httpx.Request):
        if "bad" in req.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "Dev", "absolute_url": "https://x/1", "location": {"name": "Colombia"}},
            {"id": 2, "title": "Dev US", "absolute_url": "https://x/2", "location": {"name": "Remote - US"}}]})

    companies = [{"name": "Bad", "ats": "greenhouse", "token": "bad"},
                 {"name": "Good", "ats": "greenhouse", "token": "good"}]
    logs, pauses = [], []
    with _client(handler) as c:
        jobs = list(ats.fetch_all(companies, c, log=logs.append, sleep=pauses.append))
    assert [j["id"] for j in jobs] == ["ats:greenhouse:good:1"]
    assert any("bad" in m for m in logs)
    assert pauses == [ats.DELAY_S]  # pausa entre empresas


def test_fetch_all_providers_in_parallel_same_provider_in_series():
    import threading

    lever_called = threading.Event()

    def handler(req: httpx.Request):
        if "lever" in req.url.host:
            lever_called.set()
            return httpx.Response(200, json=[{"id": "L1", "text": "QA", "hostedUrl": "https://x/L1",
                                              "categories": {"location": "Bogotá, Colombia"}}])
        # Greenhouse espera a que Lever haya arrancado: en serie, esto no pasaría nunca
        if not lever_called.wait(5):
            return httpx.Response(500)
        token = req.url.path.split("/")[3]
        return httpx.Response(200, json={"jobs": [{"id": 1, "title": "Dev", "absolute_url": f"https://x/{token}",
                                                   "location": {"name": "Colombia"}}]})

    companies = [{"name": "G1", "ats": "greenhouse", "token": "g1"},
                 {"name": "G2", "ats": "greenhouse", "token": "g2"},
                 {"name": "L", "ats": "lever", "token": "l"}]
    logs, pauses = [], []
    with _client(handler) as c:
        jobs = list(ats.fetch_all(companies, c, log=logs.append, sleep=pauses.append))
    assert logs == []
    # mismo orden que la lista, aunque Lever termine primero
    assert [j["id"] for j in jobs] == ["ats:greenhouse:g1:1", "ats:greenhouse:g2:1", "ats:lever:l:L1"]
    assert pauses == [ats.DELAY_S]  # solo entre las dos de Greenhouse, no entre proveedores


def test_fetch_lever_rejects_non_list():
    with _client(lambda r: httpx.Response(200, json={"error": "x"})) as c:
        with pytest.raises(ValueError):
            ats.fetch_lever(c, CO)


def test_fetch_smartrecruiters_uses_country_filter_and_detail():
    seen = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        if req.url.path.endswith("/postings"):
            return httpx.Response(200, json={"totalFound": 1, "content": [
                {"id": "1", "name": "Dev", "location": {"country": "co", "city": "Bogotá"}}]})
        return httpx.Response(404)  # el detalle falla: se conserva la oferta sin descripción

    with _client(handler) as c:
        jobs = ats.fetch_smartrecruiters(c, {**CO, "ats": "smartrecruiters"}, sleep=lambda s: None)
    assert "country=co" in seen[0] and len(jobs) == 1 and jobs[0]["open_to_colombia"]


def test_load_companies_file_is_valid():
    companies = ats.load_companies()
    assert len(companies) > 10
    assert all(set(c) == {"name", "ats", "token"} for c in json.load(open(ats.config.ATS_COMPANIES_FILE, encoding="utf-8")))
