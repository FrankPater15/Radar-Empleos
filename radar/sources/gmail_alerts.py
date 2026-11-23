"""Fuente (fase 1b): alertas de empleo que LinkedIn y Computrabajo te mandan a TU correo.

No es scraping de esas páginas: se leen, con la API de Gmail y permiso de solo lectura,
correos que ya llegaron a tu bandeja. Nada se borra, se marca ni se envía.

Puesta en marcha (una vez):
  1. Crea el cliente OAuth en Google Cloud (ver README, "Alertas por correo") y guarda el
     JSON descargado como secrets/gmail_credentials.json (Git ignora la carpeta secrets/).
  2. python -m radar.sources.gmail_alerts --auth        abre el navegador para autorizar
  3. python -m radar.sources.gmail_alerts --muestras 10 guarda alertas de ejemplo en
     secrets/gmail_muestras/ para escribir los lectores de cada formato.
"""
from __future__ import annotations

import argparse
import base64
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from radar import config

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]  # solo lectura


class GmailSetupError(RuntimeError):
    """Faltan las credenciales OAuth o la autorización: no se puede leer el correo."""


@dataclass
class Mail:
    id: str
    sender: str
    subject: str
    date: datetime | None
    html: str
    text: str


# --- Autorización -----------------------------------------------------------------------------

def load_credentials(credentials_file: Path | None = None, token_file: Path | None = None,
                     interactive: bool = False):
    """Credenciales OAuth guardadas (se renuevan solas). Con `interactive` abre el navegador
    para autorizar si todavía no hay token; sin él, falla con instrucciones."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    credentials_file = credentials_file or config.GMAIL_CREDENTIALS_FILE
    token_file = token_file or config.GMAIL_TOKEN_FILE
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES) if token_file.exists() else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        if not credentials_file.exists():
            raise GmailSetupError(f"No encuentro {credentials_file}. Descarga el cliente OAuth de "
                                  "Google Cloud y guárdalo ahí (ver README, 'Alertas por correo').")
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        hint = {"login_hint": config.GMAIL_ACCOUNT} if config.GMAIL_ACCOUNT else {}
        creds = flow.run_local_server(port=0, prompt="consent", **hint)
    else:
        raise GmailSetupError("Gmail no está autorizado. Corre: python -m radar.sources.gmail_alerts --auth")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


def make_service(creds=None):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds or load_credentials(), cache_discovery=False)


# --- Lectura ----------------------------------------------------------------------------------

def _b64(data: str | None) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _bodies(part: dict) -> tuple[str, str]:
    """(html, texto) de un mensaje MIME, recorriendo las partes anidadas."""
    mime = part.get("mimeType", "")
    data = (part.get("body") or {}).get("data")
    if mime == "text/html":
        return _b64(data), ""
    if mime == "text/plain":
        return "", _b64(data)
    html_parts, text_parts = [], []
    for sub in part.get("parts") or []:
        h, t = _bodies(sub)
        html_parts.append(h)
        text_parts.append(t)
    return "".join(html_parts), "".join(text_parts)


def parse_message(msg: dict) -> Mail:
    payload = msg.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or [] if "name" in h}
    ms = msg.get("internalDate")
    html, text = _bodies(payload)
    return Mail(id=msg["id"], sender=parseaddr(headers.get("from", ""))[1].lower(),
                subject=headers.get("subject", ""), html=html, text=text,
                date=datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms else None)


def search(service, query: str | None = None, max_results: int = 50) -> list[Mail]:
    """Correos que cumplen la búsqueda de Gmail (config.GMAIL_QUERY), del más nuevo al más viejo."""
    query = query or config.GMAIL_QUERY
    ids, page = [], None
    while len(ids) < max_results:
        resp = service.users().messages().list(userId="me", q=query, pageToken=page,
                                               maxResults=min(100, max_results - len(ids))).execute()
        ids += [m["id"] for m in resp.get("messages") or []]
        page = resp.get("nextPageToken")
        if not page:
            break
    return [parse_message(service.users().messages().get(userId="me", id=i, format="full").execute())
            for i in ids[:max_results]]


# --- Muestras para escribir los lectores ------------------------------------------------------

_UNSAFE = re.compile(r"[^a-z0-9]+")


def save_samples(mails: list[Mail], folder: Path) -> list[Path]:
    """Guarda cada alerta como HTML (o texto) en `folder`, que Git ignora."""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for m in mails:
        who = _UNSAFE.sub("-", m.sender.split("@")[-1]).strip("-") or "desconocido"
        path = folder / f"{who}_{m.id}.{'html' if m.html else 'txt'}"
        path.write_text(m.html or m.text, encoding="utf-8")
        out.append(path)
    return out


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Alertas de empleo de tu Gmail (solo lectura).")
    p.add_argument("--auth", action="store_true", help="autorizar el acceso de solo lectura (abre el navegador)")
    p.add_argument("--muestras", type=int, metavar="N", help="guardar N alertas de ejemplo en secrets/gmail_muestras/")
    p.add_argument("--query", default=None, help="búsqueda de Gmail (por defecto GMAIL_QUERY)")
    args = p.parse_args(argv)
    try:
        creds = load_credentials(interactive=args.auth)
        if args.auth:
            print(f"Autorizado. Token guardado en {config.GMAIL_TOKEN_FILE}")
        if args.muestras:
            mails = search(make_service(creds), args.query, args.muestras)
            for m, path in zip(mails, save_samples(mails, config.GMAIL_SAMPLES_DIR)):
                when = m.date.strftime("%Y-%m-%d") if m.date else "?"
                print(f"  {when}  {m.sender:<40}  {m.subject[:70]}")
            print(f"{len(mails)} correos guardados en {config.GMAIL_SAMPLES_DIR}")
    except GmailSetupError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
