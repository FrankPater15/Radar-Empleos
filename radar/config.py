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

# Puntuación con IA (fase 2). Credenciales: ANTHROPIC_API_KEY en el entorno o en .env
CV_PATH = os.getenv("CV_PATH", "cv/cv_maestro.md")
SCORING_MODEL = os.getenv("SCORING_MODEL", "claude-opus-5-5")
SCORING_EFFORT = os.getenv("SCORING_EFFORT", "low")  # low | medium | high

# Identificador honesto del cliente; no incluye datos personales
USER_AGENT = "radar-empleo-co/0.1 (+personal job search tool)"
