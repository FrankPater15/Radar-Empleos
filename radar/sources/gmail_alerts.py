"""Fuente (fase 1b): alertas de empleo que LinkedIn y Computrabajo te mandan a TU correo.

No es scraping de esas páginas: se leen, con la API de Gmail y permiso de solo lectura,
correos que ya llegaron a tu bandeja. Nada se borra, se marca ni se envía.

Puesta en marcha (una vez):
  1. Crea el cliente OAuth en Google Cloud (ver README, "Alertas por correo") y guarda el
     JSON descargado como secrets/gmail_credentials.json (Git ignora la carpeta secrets/).
  2. python -m radar.sources.gmail_alerts --auth        abre el navegador para autorizar
  3. python -m radar.sources.gmail_alerts --muestras 10 guarda alertas de ejemplo en
     secrets/gmail_muestras/ para escribir los lectores de cada formato.

Desde ahí la ingesta usa la fuente "gmail": cada correo se pasa al lector de su remitente
(hoy LinkedIn; los que no tienen lector, como Computrabajo por ahora, se cuentan y se omiten).
"""
from __future__ import annotations

import argparse
import base64
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Callable, Iterator

from radar import config
from radar.sources import linkedin_mail
from radar.sources.common import PARSE_ERRORS

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]  # solo lectura
logger = logging.getLogger("radar.sources")


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
    if creds and creds.expired and creds.refresh_token and not interactive:
        from google.auth.exceptions import RefreshError

        try:
            creds.refresh(Request())
        except RefreshError as e:  # en modo "Testing", Google revoca la autorización a los 7 días
            raise GmailSetupError("La autorización de Gmail venció o fue revocada. Corre: "
                                  "python -m radar.sources.gmail_alerts --auth") from e
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


# --- Fuente de la ingesta ---------------------------------------------------------------------

# Dominio del remitente -> lector de ese formato de correo
READERS: dict[str, Callable[[str, datetime | None], list[dict]]] = {
    "linkedin.com": linkedin_mail.parse_alert,
}


def reader_for(sender: str):
    domain = sender.rsplit("@", 1)[-1]
    return next((r for d, r in READERS.items() if domain == d or domain.endswith("." + d)), None)


def is_authorized(token_file: Path | None = None) -> bool:
    return (token_file or config.GMAIL_TOKEN_FILE).exists()


def fetch_all(service=None, query: str | None = None, max_results: int | None = None,
              log=print) -> Iterator[dict]:
    """Ofertas de las alertas del correo. Se recorren de la más vieja a la más nueva, así una
    oferta que llega en varios correos conserva la fecha del primero."""
    service = service or make_service()
    mails = search(service, query, max_results or config.GMAIL_MAX_MESSAGES)
    stats = Counter()
    for mail in reversed(mails):
        read = reader_for(mail.sender)
        if read is None:
            stats["sin lector"] += 1
            continue
        try:
            jobs = read(mail.html, mail.date)
        except PARSE_ERRORS as e:  # un correo raro no tumba los demás
            logger.warning("gmail: correo %s omitido (%s: %s)", mail.id, type(e).__name__, e)
            stats["ilegibles"] += 1
            continue
        stats["leídos"] += 1
        stats["ofertas"] += len(jobs)
        yield from jobs
    extra = "".join(f", {stats[k]} {k}" for k in ("sin lector", "ilegibles") if stats[k])
    log(f"  gmail: {len(mails)} correos ({stats['leídos']} leídos{extra}), {stats['ofertas']} ofertas")


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
