"""Fase 1b: lectura de alertas de Gmail, con un servicio falso (no toca ningún correo real)."""
import base64
import json
from contextlib import nullcontext
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from radar import ingest
from radar.db import init_db, make_engine
from radar.models import Job
from radar.sources import gmail_alerts as gm
from radar.sources import linkedin_mail as li


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")  # Gmail omite el relleno


def message(i: str, html: str = "", text: str = "", sender: str = "LinkedIn <jobalerts-noreply@linkedin.com>",
            subject: str = "QA Junior: 3 empleos nuevos"):
    parts = []
    if text:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(text)}})
    if html:
        parts.append({"mimeType": "multipart/alternative",
                      "parts": [{"mimeType": "text/html", "body": {"data": b64(html)}}]})
    return {"id": i, "internalDate": "1795000000000",
            "payload": {"mimeType": "multipart/mixed", "parts": parts,
                        "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]}}


class FakeGmail:
    """Imita service.users().messages().list/get(...).execute() con paginación."""

    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.queries = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, pageToken, maxResults):
        assert userId == "me"
        self.queries.append(q)
        n = int(pageToken or 0)
        resp = {"messages": [{"id": m["id"]} for m in self.pages[n]]}
        if n + 1 < len(self.pages):
            resp["nextPageToken"] = str(n + 1)
        return _Exec(resp)

    def get(self, userId, id, format):
        return _Exec(next(m for page in self.pages for m in page if m["id"] == id))


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def test_parse_message_reads_nested_html_text_and_headers():
    m = gm.parse_message(message("a", html="<p>Analista QA – Bogotá</p>", text="Analista QA"))
    assert m.sender == "jobalerts-noreply@linkedin.com"
    assert m.subject == "QA Junior: 3 empleos nuevos"
    assert "Bogotá" in m.html and m.text == "Analista QA"
    assert m.date.year == 2026


def test_search_follows_pages_and_stops_at_max():
    svc = FakeGmail([[message("1", html="x"), message("2", html="y")], [message("3", html="z")]])
    assert [m.id for m in gm.search(svc, "from:linkedin.com", max_results=10)] == ["1", "2", "3"]
    assert svc.queries == ["from:linkedin.com", "from:linkedin.com"]
    assert [m.id for m in gm.search(FakeGmail(svc.pages), "q", max_results=2)] == ["1", "2"]


def test_save_samples_names_by_sender_domain(tmp_path):
    mails = [gm.parse_message(message("abc", html="<b>hola</b>")),
             gm.parse_message(message("def", text="solo texto", sender="alertas@computrabajo.com.co"))]
    paths = gm.save_samples(mails, tmp_path / "muestras")
    assert [p.name for p in paths] == ["linkedin-com_abc.html", "computrabajo-com-co_def.txt"]
    assert paths[1].read_text(encoding="utf-8") == "solo texto"


def test_without_token_asks_to_authorize_instead_of_opening_browser(tmp_path):
    with pytest.raises(gm.GmailSetupError, match="--auth"):
        gm.load_credentials(tmp_path / "cred.json", tmp_path / "token.json", interactive=False)


def test_auth_without_client_file_explains_where_to_put_it(tmp_path):
    with pytest.raises(gm.GmailSetupError, match="cred.json"):
        gm.load_credentials(tmp_path / "cred.json", tmp_path / "token.json", interactive=True)


def test_only_read_only_scope_is_requested():
    assert gm.SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_expired_authorization_asks_to_authorize_again(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                                 "token_uri": "https://oauth2.googleapis.com/token",
                                 "expiry": "2020-01-01T00:00:00Z", "scopes": gm.SCOPES}))

    def revoked(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", revoked)
    with pytest.raises(gm.GmailSetupError, match="venció"):
        gm.load_credentials(tmp_path / "cred.json", token, interactive=False)


# --- Lector de alertas de LinkedIn ------------------------------------------------------------
# Estructura copiada de correos reales; nombres, enlaces y destinatario son inventados.

def card(job_id: str, title: str, detail: str, extra: str = "") -> str:
    href = f"https://www.linkedin.com/comm/jobs/view/{job_id}/?trackingId=abc%3D&refId=xyz&lipi=urn"
    return f"""
<tr><td><a href="{href}"><img alt="Logo" src="https://media.licdn.com/x.png"></a></td>
<td><a href="{href}"><table><tr><td>
  <a href="{href}" style="color:#0a66c2">{title}</a>
  <p>{detail}</p>
  {extra}
</td></tr></table></a></td></tr>"""


ALERT = f"""<html><body>
<div style="display:none">Acme Software Engineer: ¡Únete!&#847; &#847; &#847;</div>
<a href="https://www.linkedin.com/comm/feed/"><img alt="LinkedIn"></a>
<h1><a href="https://www.linkedin.com/comm/jobs/view/111/?refId=h">Empleos similares a <strong>Analista QA</strong>
en <strong>Acme</strong></a></h1>
<table>
{card("222", "Desarrollador Junior Python", "Acme S.A.S. · Medellín (Híbrido)",
      '<img alt="Solicitud sencilla"> Solicitud sencilla')}
{card("333", "QA Automation &amp; Testing", "Beta Labs · Colombia (En remoto)")}
{card("444", "Python Developer, Medellin-Colombia", "Gamma · Medellín, Antioquia, Colombia")}
{card("222", "Desarrollador Junior Python", "Acme S.A.S. · Medellín (Híbrido)")}
</table>
<a href="https://www.linkedin.com/comm/jobs/view/444/?x=1"><a href="https://www.linkedin.com/comm/jobs/view/444/">Solicitar</a></a>
<h2>Tus otros empleos guardados</h2>
<p>Este email está dirigido a Persona Ejemplo (Dev · Python · React)</p>
<a href="https://www.linkedin.com/comm/mypreferences/u/emailunsub">Cancelar suscripción</a>
</body></html>"""


def test_linkedin_alert_reads_each_card_once():
    when = datetime(2026, 11, 20, tzinfo=timezone.utc)
    jobs = li.parse_alert(ALERT, when)
    assert [j["external_id"] for j in jobs] == ["222", "333", "444"]  # sin encabezado, botón ni repetidas
    a, b, c = jobs
    assert a["id"] == "linkedin:222" and a["source"] == "linkedin"
    assert (a["title"], a["company"]) == ("Desarrollador Junior Python", "Acme S.A.S.")
    assert a["remote_modality"] == "hybrid" and a["countries"] == ["Medellín", "Colombia"]
    assert a["url"] == "https://www.linkedin.com/jobs/view/222/"  # sin parámetros de rastreo
    assert a["published_at"] == when and a["open_to_colombia"] is True
    assert "Medellín (híbrido)" in a["description"]  # el prefiltro ve la ciudad
    assert b["title"] == "QA Automation & Testing" and b["remote_modality"] == "fully_remote"
    assert b["countries"] == ["Colombia"]
    assert c["remote_modality"] is None and c["countries"] == ["Medellín, Antioquia, Colombia"]


def test_linkedin_alert_without_cards_gives_nothing():
    assert li.parse_alert("<p>Tus publicaciones obtuvieron 114 impresiones · ver más</p>") == []
    assert li.parse_alert("") == []


def test_reader_is_chosen_by_exact_sender_domain():
    assert gm.reader_for("jobalerts-noreply@linkedin.com") is li.parse_alert
    assert gm.reader_for("x@mail.linkedin.com") is li.parse_alert
    assert gm.reader_for("x@fake-linkedin.com") is None
    assert gm.reader_for("sin-respuesta@computrabajo.com") is None  # aún sin lector


def test_fetch_all_reads_oldest_first_and_skips_senders_without_reader():
    newest = message("n", html=ALERT)
    newest["internalDate"] = "1795000000000"
    oldest = message("o", html=card("222", "Desarrollador Junior Python", "Acme S.A.S. · Medellín (Híbrido)"))
    oldest["internalDate"] = "1790000000000"
    welcome = message("w", html="<p>Bienvenida</p>", sender="sin-respuesta@computrabajo.com")
    logs = []
    jobs = list(gm.fetch_all(FakeGmail([[welcome, newest, oldest]]), "q", 10, log=logs.append))
    assert [j["external_id"] for j in jobs] == ["222", "222", "333", "444"]
    assert jobs[0]["published_at"].timestamp() == 1790000000  # el primer correo que la trajo
    assert logs == ["  gmail: 3 correos (2 leídos, 1 sin lector), 4 ofertas"]


def test_gmail_source_ingests_jobs_into_db(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{(tmp_path / 'r.db').as_posix()}")
    init_db(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(gm, "is_authorized", lambda token_file=None: True)
    monkeypatch.setattr(gm, "make_service", lambda creds=None: FakeGmail([[message("n", html=ALERT)]]))
    errors = {}
    results = ingest.run(["gmail"], log=lambda m: None, session_factory=Session,
                         client_factory=nullcontext, errors=errors)
    assert results == {"gmail": (3, 0)} and errors == {}
    with Session() as s:
        junior = s.get(Job, "linkedin:222")
        assert junior.company == "Acme S.A.S." and junior.fit >= 60
    engine.dispose()


def test_gmail_source_is_skipped_until_authorized(monkeypatch):
    monkeypatch.setattr(gm, "is_authorized", lambda token_file=None: False)
    logs = []
    assert list(ingest.source_streams(["gmail"], None, logs.append)) == []
    assert "--auth" in logs[0]
