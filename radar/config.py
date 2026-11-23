"""Configuración leída de variables de entorno (o de un archivo .env)."""
import os

from dotenv import load_dotenv

load_dotenv()

# SQLite por defecto; para Postgres/Neon: postgresql+psycopg://usuario:clave@host/db
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///radar.db")

# Categorías de Get on Board a consultar (ver /api/v0/categories)
GETONBRD_CATEGORIES = [
    c.strip()
    for c in os.getenv(
        "GETONBRD_CATEGORIES", "programming,technical-support,sysadmin-devops-qa"
    ).split(",")
    if c.strip()
]

# Páginas por categoría en cada ingesta (la API devuelve lo más reciente primero)
GETONBRD_MAX_PAGES = int(os.getenv("GETONBRD_MAX_PAGES", "5"))
GETONBRD_PER_PAGE = int(os.getenv("GETONBRD_PER_PAGE", "50"))


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


# Fuentes activas (todas son APIs públicas que permiten este uso)
SOURCES = _list("SOURCES", "getonbrd,himalayas,jobicy")

# Búsquedas de texto en Get on Board y Himalayas (Himalayas ya filtra a ofertas abiertas a Colombia)
SEARCH_QUERIES = _list(
    "SEARCH_QUERIES",
    "junior developer,desarrollador junior,trainee,practicante,entry level,qa automation,"
    "rpa,automatizacion,soporte de aplicaciones,implementacion de software,full stack,"
    "python,react,node,angular,flutter,.net",
)
HIMALAYAS_MAX_PAGES = int(os.getenv("HIMALAYAS_MAX_PAGES", "3"))  # 20 ofertas por página
JOBICY_INDUSTRIES = _list("JOBICY_INDUSTRIES", "engineering,qa-testing,technical-support,data-science")

# Prefiltro local: habilidades de tu CV que suman puntos, y nota mínima para gastar IA
SKILLS = _list(
    "SKILLS",
    "python,javascript,typescript,node,react,angular,flutter,dart,c#,.net,sql,"
    "mongodb,supabase,rpa,ocr,rest,git,ci/cd,pruebas automatizadas,testing,qa,"
    "microsoft graph,express,tailwind,vite,azure",
)
MIN_FIT = int(os.getenv("MIN_FIT", "45"))

# Puntuación con IA (fase 2). Credenciales en .env: GEMINI_API_KEY o ANTHROPIC_API_KEY
CV_PATH = os.getenv("CV_PATH", "cv/cv_maestro.md")
SCORING_PROVIDER = os.getenv("SCORING_PROVIDER", "gemini")  # gemini | anthropic
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite")
SCORING_MODEL = os.getenv("SCORING_MODEL", "claude-opus-5-5")
SCORING_EFFORT = os.getenv("SCORING_EFFORT", "low")  # low | medium | high

# Identificador honesto del cliente; no incluye datos personales
USER_AGENT = "radar-empleo-co/0.1 (+personal job search tool)"
