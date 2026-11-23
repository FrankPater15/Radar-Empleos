"""Aviso de escritorio al terminar la corrida diaria (win11toast). Si no está disponible, solo log."""
from __future__ import annotations

PANEL_NEW_URL = "http://127.0.0.1:8000/?nuevas=1"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def format_message(new: int, promising: int) -> tuple[str, str]:
    title = "Radar de Empleo CO"
    body = f"{_plural(new, 'oferta nueva', 'ofertas nuevas')}, {_plural(promising, 'prometedora', 'prometedoras')}"
    if new:
        body += ". Clic para revisarlas en el panel."
    return title, body


def notify_desktop(title: str, body: str, url: str = PANEL_NEW_URL, log=print) -> bool:
    """Muestra el toast sin bloquear; el clic abre el panel. Devuelve False si no se pudo."""
    try:
        from win11toast import notify
    except Exception as e:  # no instalado, otro sistema operativo, WinRT roto…
        log(f"Aviso de escritorio no disponible ({type(e).__name__}): {body}")
        return False
    try:
        notify(title, body, on_click=url)
    except Exception as e:
        log(f"No se pudo mostrar el aviso de escritorio ({type(e).__name__}: {e}): {body}")
        return False
    return True
