"""Fuente: tableros de empleo públicos de ATS (Greenhouse, Lever, Ashby, SmartRecruiters,
Workable, BambooHR) de una lista curada de empresas (ats_companies.json), más RemoteOK.

Son los endpoints que cada proveedor publica para que las empresas muestren sus vacantes.
Cada oferta enlaza a su URL original en el sitio de la empresa. Solo se conservan las
ofertas abiertas a Colombia (país CO, ciudad colombiana, LATAM o remoto desde cualquier lugar).
Cada empresa falla por separado. Los proveedores se consultan a la vez (son servidores
distintos), pero dentro de cada uno las empresas van de a una, con una pausa de cortesía
entre ellas: cada servidor recibe el mismo ritmo de peticiones que si todo fuera en serie.
"""
from __future__ import annotations

import html
import json
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Iterator

import httpx

from radar import config
from radar.sources.common import PARSE_ERRORS, get_json, html_to_text, logger, make_client, parse_each, to_int

SOURCE = "ats"
SOURCE_NAME = "Empresas (ATS)"
REMOTEOK_SOURCE = "remoteok"
REMOTEOK_NAME = "Remote OK"
MAX_DESCRIPTION = 8000
DELAY_S = 1.0

_CO_STRICT = re.compile(r"colombia|medell[ií]n|bogot[aá]|\bcali\b|barranquilla|cartagena|bucaramanga", re.I)
_LATAM = re.compile(r"latam|latin america|latinoam[eé]rica|south america|am[eé]rica latina", re.I)
_WORLD = re.compile(r"anywhere|worldwide|world wide|global|cualquier", re.I)

_JUNIOR = re.compile(r"\b(junior|jr\.?|trainee|intern|internship|practicante|pasante|aprendiz|entry[- ]level|"
                     r"graduate|semillero)\b", re.I)
_SENIOR = re.compile(r"\b(senior|sr\.?|lead|staff|principal|head|director|manager|architect)\b", re.I)


def geo_ats(places: list[str | None], country: str | None = None, remote: bool | None = None) -> bool:
    """¿La oferta está abierta a Colombia?

    - país CO o ciudad colombiana en cualquier ubicación: sí
    - LATAM / Latin America / South America: sí
    - 'Anywhere/Worldwide/Global': sí, salvo que se sepa que no es remota
    - 'Americas' a secas, 'Remote' sin país, 'US only': no
    """
    if (country or "").strip().lower() in ("co", "col", "colombia"):
        return True
    text = " | ".join(p for p in places if p)
    if _CO_STRICT.search(text) or _LATAM.search(text):
        return True
    return bool(_WORLD.search(text)) and remote is not False


def _level_from_title(title: str) -> str | None:
    if _JUNIOR.search(title):
        return "Junior"
    if _SENIOR.search(title):
        return "Senior"
    return None


def _clip(text: str) -> str:
    return text[:MAX_DESCRIPTION]


def _dt_ms(ms) -> datetime | None:
    n = to_int(ms)
    return datetime.fromtimestamp(n / 1000, tz=timezone.utc) if n else None


def _dt_iso(s) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _modality(remote: bool | None, hybrid: bool = False) -> str | None:
    if hybrid:
        return "hybrid"
    if remote is None:
        return None
    return "fully_remote" if remote else "no_remote"


def _label(ats: str, company: dict) -> str:
    return f"ats {ats}/{company['token']}"


def _base(ats: str, company: dict, jobid, title, url, *, open_to_colombia: bool, countries=None,
          category=None, seniority=None, remote_modality=None, description="", published_at=None) -> dict:
    token = company["token"]
    return {
        "id": f"{SOURCE}:{ats}:{token}:{jobid}",
        "source": SOURCE,
        "source_name": f"{company['name']} (ATS)",
        "external_id": f"{ats}:{token}:{jobid}",
        "title": (title or "(sin título)").strip(),
        "company": company["name"],
        "url": url,
        "category": category,
        "seniority": seniority or _level_from_title(title or ""),
        "remote_modality": remote_modality,
        "countries": countries or ["Colombia"],
        "min_salary": None, "max_salary": None, "salary_currency": None,
        "description": _clip(description or ""),
        "published_at": published_at,
        "open_to_colombia": open_to_colombia,
    }


# --- Greenhouse -------------------------------------------------------------------------------

def parse_greenhouse(j: dict, company: dict) -> dict:
    loc = (j.get("location") or {}).get("name") or ""
    # `offices` suele listar oficinas de la empresa ajenas al puesto (p. ej. Bogotá en una vacante de Perú):
    # solo se usan si la oferta no trae ubicación propia
    places = [loc] if loc else [o.get("name") or "" for o in j.get("offices") or []]
    remote = True if re.search(r"remot", loc, re.I) else None
    cats = [d.get("name") for d in j.get("departments") or [] if d.get("name")]
    return _base("greenhouse", company, j["id"], j.get("title"), j.get("absolute_url"),
                 countries=[p for p in places if p][:5] or None,
                 category=cats[0] if cats else None,
                 remote_modality=_modality(remote),
                 description=html_to_text(html.unescape(j.get("content") or "")),  # viene escapado
                 published_at=_dt_iso(j.get("first_published") or j.get("updated_at")),
                 open_to_colombia=geo_ats(places, remote=remote))


def fetch_greenhouse(client, company):
    data = get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{company['token']}/jobs",
                    {"content": "true"})
    return list(parse_each(data.get("jobs"), parse_greenhouse, company, label=_label("greenhouse", company)))


# --- Lever ------------------------------------------------------------------------------------

def parse_lever(j: dict, company: dict) -> dict:
    cat = j.get("categories") or {}
    places = [cat.get("location")] + list(cat.get("allLocations") or [])
    wt = (j.get("workplaceType") or "").lower()
    remote = True if wt == "remote" else (False if wt in ("onsite", "hybrid") else None)
    lists = "\n".join(f"{x.get('text', '')}\n{html_to_text(x.get('content'))}" for x in j.get("lists") or [])
    desc = "\n".join(p for p in (j.get("descriptionPlain"), lists, j.get("additionalPlain")) if p)
    return _base("lever", company, j["id"], j.get("text"), j.get("hostedUrl"),
                 countries=[p for p in places if p] or None, category=cat.get("team"),
                 remote_modality=_modality(remote, wt == "hybrid"),
                 description=desc, published_at=_dt_ms(j.get("createdAt")),
                 open_to_colombia=geo_ats(places, country=j.get("country"), remote=remote))


def fetch_lever(client, company):
    data = get_json(client, f"https://api.lever.co/v0/postings/{company['token']}", {"mode": "json"})
    if not isinstance(data, list):
        raise ValueError("respuesta inesperada de Lever")
    return list(parse_each(data, parse_lever, company, label=_label("lever", company)))


# --- Ashby ------------------------------------------------------------------------------------

def parse_ashby(j: dict, company: dict) -> dict:
    places = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
    country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
    wt = (j.get("workplaceType") or "").lower()
    remote = j.get("isRemote")
    if remote is None and wt:
        remote = wt == "remote"
    return _base("ashby", company, j["id"], j.get("title"), j.get("jobUrl") or j.get("applyUrl"),
                 countries=[p for p in places if p] or None, category=j.get("department") or j.get("team"),
                 remote_modality=_modality(remote, wt == "hybrid"),
                 description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                 published_at=_dt_iso(j.get("publishedAt")),
                 open_to_colombia=geo_ats(places, country=country, remote=remote))


def fetch_ashby(client, company):
    data = get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{company['token']}")
    jobs = [j for j in data.get("jobs") or [] if not isinstance(j, dict) or j.get("isListed", True)]
    return list(parse_each(jobs, parse_ashby, company, label=_label("ashby", company)))


# --- SmartRecruiters --------------------------------------------------------------------------

SR_LEVELS = {"internship": "Junior", "entry_level": "Junior", "associate": "Semi Senior",
             "mid_senior_level": "Senior", "director": "Expert", "executive": "Expert"}


def parse_smartrecruiters(j: dict, company: dict, detail: dict | None = None) -> dict:
    loc = j.get("location") or {}
    full = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
    sections = ((detail or {}).get("jobAd") or {}).get("sections") or {}
    desc = "\n".join(html_to_text(s.get("text")) for s in sections.values() if isinstance(s, dict))
    url = (detail or {}).get("postingUrl") or f"https://jobs.smartrecruiters.com/{company['token']}/{j['id']}"
    return _base("smartrecruiters", company, j["id"], j.get("name"), url,
                 countries=[full] if full else None,
                 category=(j.get("department") or {}).get("label"),
                 seniority=SR_LEVELS.get(((j.get("experienceLevel") or {}).get("id") or "").lower()),
                 remote_modality=_modality(bool(loc.get("remote")), bool(loc.get("hybrid"))),
                 description=desc, published_at=_dt_iso(j.get("releasedDate")),
                 open_to_colombia=geo_ats([full], country=loc.get("country"), remote=loc.get("remote")))


def fetch_smartrecruiters(client, company, sleep=time.sleep):
    out, offset = [], 0
    base = f"https://api.smartrecruiters.com/v1/companies/{company['token']}/postings"
    while offset < 500:
        data = get_json(client, base, {"limit": 100, "offset": offset, "country": "co"})
        items = data.get("content") or []
        for j in items:
            try:  # la descripción completa solo viene en el detalle
                detail = get_json(client, f"{base}/{j['id']}")
            except Exception:
                detail = None
            out.extend(parse_each([j], parse_smartrecruiters, company, detail,
                                  label=_label("smartrecruiters", company)))
            sleep(0.3)
        offset += len(items)
        if not items or offset >= (data.get("totalFound") or 0):
            break
    return out


# --- Workable (widget) ------------------------------------------------------------------------

def parse_workable(j: dict, company: dict) -> dict:
    places = [j.get("country"), j.get("city"), j.get("state")]
    for loc in j.get("locations") or []:
        if isinstance(loc, dict):
            places.append(" ".join(str(loc.get(k) or "") for k in ("city", "region", "country")))
    remote = j.get("telecommuting")
    where = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
    return _base("workable", company, j.get("shortcode") or j.get("id"), j.get("title"),
                 j.get("url") or j.get("shortlink"),
                 countries=[where] if where else None, category=j.get("department"),
                 remote_modality=_modality(remote),
                 description=html_to_text(j.get("description")),
                 published_at=_dt_iso(j.get("published_on") or j.get("created_at")),
                 open_to_colombia=geo_ats(places, remote=remote))


def fetch_workable(client, company):
    data = get_json(client, f"https://apply.workable.com/api/v1/widget/accounts/{company['token']}",
                    {"details": "true"})
    return list(parse_each(data.get("jobs"), parse_workable, company, label=_label("workable", company)))


# --- BambooHR ---------------------------------------------------------------------------------

def parse_bamboohr(j: dict, company: dict, detail: dict | None = None) -> dict:
    loc, ats_loc = j.get("location") or {}, j.get("atsLocation") or {}
    places = [loc.get("city"), loc.get("state"), ats_loc.get("country"), ats_loc.get("city"), ats_loc.get("state")]
    remote = j.get("isRemote")
    opening = ((detail or {}).get("result") or {}).get("jobOpening") or {}
    where = ", ".join(p for p in (loc.get("city"), ats_loc.get("country")) if p)
    return _base("bamboohr", company, j["id"], j.get("jobOpeningName"),
                 f"https://{company['token']}.bamboohr.com/careers/{j['id']}",
                 countries=[where] if where else None, category=j.get("departmentLabel"),
                 remote_modality=_modality(None if remote is None else bool(remote)),
                 description=html_to_text(opening.get("description")),
                 published_at=_dt_iso(opening.get("datePosted")),
                 open_to_colombia=geo_ats(places, remote=remote))


def fetch_bamboohr(client, company, sleep=time.sleep):
    base = f"https://{company['token']}.bamboohr.com/careers"
    resp = client.get(f"{base}/list", headers={"Accept": "application/json"})
    resp.raise_for_status()
    out = []
    for j in resp.json().get("result") or []:
        try:
            job = parse_bamboohr(j, company)
        except PARSE_ERRORS as e:
            logger.warning("%s: oferta omitida por datos corruptos (%s: %s)", _label("bamboohr", company),
                           type(e).__name__, e)
            continue
        if job["open_to_colombia"]:
            try:
                r = client.get(f"{base}/{j['id']}/detail", headers={"Accept": "application/json"})
                r.raise_for_status()
                job = parse_bamboohr(j, company, r.json())
            except Exception:
                pass
            sleep(0.3)
        out.append(job)
    return out


FETCHERS: dict[str, Callable] = {
    "greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters, "workable": fetch_workable, "bamboohr": fetch_bamboohr,
}


# --- RemoteOK (API pública; exige citar "Remote OK" y enlazar a su URL) -----------------------

def parse_remoteok(j: dict) -> dict:
    loc = j.get("location") or ""
    places = [p.strip() for p in re.split(r"[,;/]", loc) if p.strip()]
    epoch = to_int(j.get("epoch"))
    lo, hi = to_int(j.get("salary_min")), to_int(j.get("salary_max"))
    open_co = not loc.strip() or geo_ats(places, remote=True)  # vacío = remoto en cualquier lugar
    title = j.get("position") or "(sin título)"
    return {
        "id": f"{REMOTEOK_SOURCE}:{j['id']}", "source": REMOTEOK_SOURCE, "source_name": REMOTEOK_NAME,
        "external_id": str(j["id"]), "title": title.strip(), "company": j.get("company"),
        "url": j.get("url") or j.get("apply_url"), "category": (j.get("tags") or [None])[0],
        "seniority": _level_from_title(title), "remote_modality": "fully_remote",
        "countries": places or ["Cualquier país"],
        "min_salary": round(lo / 12) if lo else None, "max_salary": round(hi / 12) if hi else None,
        "salary_currency": "USD" if (lo or hi) else None,
        "description": _clip(html_to_text(j.get("description"))),
        "published_at": datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch else None,
        "open_to_colombia": open_co,
    }


def fetch_remoteok(client=None, log=print) -> Iterator[dict]:
    own = client is None
    client = client or make_client()
    try:
        for j in get_json(client, "https://remoteok.com/api"):
            if isinstance(j, dict) and j.get("id") and j.get("position"):
                for job in parse_each([j], parse_remoteok, label="remoteok"):
                    if job["open_to_colombia"] and job["url"]:
                        yield job
    except Exception as e:
        log(f"  ! remoteok: {e}")
    finally:
        if own:
            client.close()


# --- Orquestación -----------------------------------------------------------------------------

def load_companies(path: str | None = None) -> list[dict]:
    with open(path or config.ATS_COMPANIES_FILE, encoding="utf-8") as f:
        return [c for c in json.load(f) if c.get("name") and c.get("ats") in FETCHERS and c.get("token")]


def fetch_company(client, company: dict) -> list[dict]:
    """Ofertas abiertas a Colombia de una empresa."""
    return [j for j in FETCHERS[company["ats"]](client, company) if j["open_to_colombia"] and j["url"]]


def _fetch_provider(client, group: list[tuple[int, dict]], log, sleep, delay_s: float) -> dict[int, list[dict]]:
    """Empresas de un mismo proveedor, en serie y con pausa: {posición en la lista: ofertas}."""
    out: dict[int, list[dict]] = {}
    for n, (i, c) in enumerate(group):
        if n:
            sleep(delay_s)
        try:
            out[i] = fetch_company(client, c)
        except Exception as e:  # una empresa caída no tumba la corrida
            log(f"  ! ats {c.get('ats')}/{c.get('token')}: {type(e).__name__}: {e}")
    return out


def fetch_all(companies: list[dict] | None = None, client: httpx.Client | None = None, log=print,
              sleep=time.sleep, delay_s: float = DELAY_S) -> Iterator[dict]:
    own = client is None
    client = client or make_client()
    try:
        companies = load_companies() if companies is None else companies
        by_ats: dict[str, list[tuple[int, dict]]] = defaultdict(list)
        for i, c in enumerate(companies):
            by_ats[c.get("ats")].append((i, c))
        with ThreadPoolExecutor(max_workers=max(1, len(by_ats)), thread_name_prefix="ats") as pool:
            futures = [pool.submit(_fetch_provider, client, group, log, sleep, delay_s)
                       for group in by_ats.values()]
        results: dict[int, list[dict]] = {}
        for f in futures:
            results.update(f.result())
        for i in sorted(results):  # mismo orden que la lista de empresas
            yield from results[i]
    finally:
        if own:
            client.close()
