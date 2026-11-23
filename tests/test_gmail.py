"""Fase 1b: lectura de alertas de Gmail, con un servicio falso (no toca ningún correo real)."""
import base64

import pytest

from radar.sources import gmail_alerts as gm


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
