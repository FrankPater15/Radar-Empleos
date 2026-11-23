"""ConfiguraciÃ³n leÃ­da de variables de entorno (o de un archivo .env)."""
import os
from pathlib import Path

from dotenv import load_dotenv

# RaÃ­z del proyecto: las rutas relativas se resuelven contra ella y no contra el directorio
# de trabajo, para que la tarea programada funcione arranque donde arranque.
ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


def project_path(p: str | Path) -> Path:
    """Ruta absoluta: si es relativa, relativa a la raÃ­z del proyecto."""
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def resolve_db_url(url: str) -> str:
    """Convierte 'sqlite:///radar.db' (ruta relativa) en una ruta absoluta dentro del proyecto."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return url  # Postgres, sqlite:// en memoria, etc.
    path = url[len(prefix):]
    if not path or path == ":memory:" or path.startswith("/") or Path(path).is_absolute():
        return url
    return prefix + project_path(path).as_posix()


# SQLite por defecto; para Postgres/Neon: postgresql+psycopg://usuario:clave@host/db
DATABASE_URL = resolve_db_url(os.getenv("DATABASE_URL", "sqlite:///radar.db"))

# Log de las corridas (con rotaciÃ³n) y espera mÃ¡xima de red antes de rendirse
LOG_FILE = project_path(os.getenv("LOG_FILE", "logs/radar.log"))
WAIT_NETWORK_S = int(os.getenv("WAIT_NETWORK_S", "180"))

# CategorÃ­as de Get on Board a consultar (ver /api/v0/categories)
GETONBRD_CATEGORIES = [
    c.strip()
    for c in os.getenv(
        "GETONBRD_CATEGORIES", "programming,technical-support,sysadmin-devops-qa"
    ).split(",")
    if c.strip()
]

# PÃ¡ginas por categorÃ­a en cada ingesta (la API devuelve lo mÃ¡s reciente primero)
GETONBRD_MAX_PAGES = int(os.getenv("GETONBRD_MAX_PAGES", "5"))
GETONBRD_PER_PAGE = int(os.getenv("GETONBRD_PER_PAGE", "50"))


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


# Fuentes activas (todas son APIs pÃºblicas que permiten este uso)
SOURCES = _list("SOURCES", "getonbrd,himalayas,jobicy,ats")

# BÃºsquedas de texto en Get on Board y Himalayas (Himalayas ya filtra a ofertas abiertas a Colombia)
SEARCH_QUERIES = _list(
    "SEARCH_QUERIES",
    "junior developer,desarrollador junior,trainee,practicante,entry level,qa automation,"
    "rpa,automatizacion,soporte de aplicaciones,implementacion de software,full stack,"
    "python,react,node,angular,flutter,.net",
)
HIMALAYAS_MAX_PAGES = int(os.getenv("HIMALAYAS_MAX_PAGES", "3"))  # 20 ofertas por pÃ¡gina
JOBICY_INDUSTRIES = _list("JOBICY_INDUSTRIES", "engineering,qa-testing,technical-support,data-science")

# Prefiltro local: habilidades de tu CV que suman puntos, y nota mÃ­nima para gastar IA
SKILLS = _list(
    "SKILLS",
    "python,javascript,typescript,node,react,angular,flutter,dart,c#,.net,sql,"
    "mongodb,supabase,rpa,ocr,rest,git,ci/cd,pruebas automatizadas,testing,qa,"
    "microsoft graph,express,tailwind,vite,azure",
)
MIN_FIT = int(os.getenv("MIN_FIT", "45"))
# Panel: cargar las tipografías de Google Fonts (contacta a Google en cada visita). 0 = fuentes del sistema
GOOGLE_FONTS = os.getenv("GOOGLE_FONTS", "1").strip().lower() not in ("0", "false", "no", "off")

# PuntuaciÃ³n con IA (fase 2). Credenciales en .env: GEMINI_API_KEY o ANTHROPIC_API_KEY
CV_PATH = str(project_path(os.getenv("CV_PATH", "cv/cv_maestro.md")))
SCORING_PROVIDER = os.getenv("SCORING_PROVIDER", "gemini")  # gemini | anthropic
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite")
SCORING_MODEL = os.getenv("SCORING_MODEL", "claude-opus-5-5")
SCORING_EFFORT = os.getenv("SCORING_EFFORT", "low")  # low | medium | high

# Identificador honesto del cliente; no incluye datos personales
USER_AGENT = "radar-empleo-co/0.1 (+personal job search tool)"

# ---------- Ranking v2 ----------
# Nivel de inglÃ©s del CV (A1..C2): las ofertas que piden mÃ¡s restan puntos en el prefiltro y la rÃºbrica
ENGLISH_LEVEL = os.getenv("ENGLISH_LEVEL", "A2").upper()
# Â¿Eres estudiante activo? Las prÃ¡cticas que lo exigen restan si no lo eres
IS_STUDENT = os.getenv("IS_STUDENT", "false").lower() in ("1", "true", "si", "sÃ­", "yes")
# VersiÃ³n de la rÃºbrica de IA: al cambiarla, todas las notas quedan pendientes de re-puntuar
RUBRIC_VERSION = "v2"
# PuntuaciÃ³n por lotes y presupuesto local de peticiones por minuto (0 = sin lÃ­mite)
SCORING_BATCH_SIZE = int(os.getenv("SCORING_BATCH_SIZE", "6"))
GEMINI_RPM = int(os.getenv("GEMINI_RPM", "10"))
# Tras caer al modelo de respaldo, volver a probar el principal cada N llamadas
GEMINI_PRIMARY_RETRY_EVERY = int(os.getenv("GEMINI_PRIMARY_RETRY_EVERY", "10"))
# No gastar IA en ofertas publicadas hace mÃ¡s de N dÃ­as
MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS", "90"))
# Una oferta que falla N veces (JSON inválido, rechazo del modelo…) con el mismo CV deja de
# reintentarse cada día y de gastar cuota; `python -m radar.score --retry-failed` la reintenta
SCORE_MAX_FAILURES = int(os.getenv("SCORE_MAX_FAILURES", "3"))
# rank_value: dÃ­as sin descuento por antigÃ¼edad y vida media despuÃ©s
RANK_FRESH_DAYS = 14
RANK_HALF_LIFE_DAYS = 30
# Hallazgos de mercado: habilidades y bÃºsquedas extra (si no las definiste en .env)
if not os.getenv("SKILLS"):
    SKILLS = SKILLS + ["java", "spring", "power bi"]
if not os.getenv("SEARCH_QUERIES"):
    SEARCH_QUERIES = SEARCH_QUERIES + [
        "analista desarrollador", "analista de pruebas", "tester junior", "desarrollador rpa", "uipath",
        "power automate", "associate software engineer", "software engineer i", "new grad",
        "junior data analyst", "aprendiz desarrollo de software"]
# Fuente ATS: tableros publicos de empresas (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, BambooHR)
ATS_COMPANIES_FILE = str(project_path(os.getenv("ATS_COMPANIES_FILE", "radar/sources/ats_companies.json")))
ATS_ENABLED = os.getenv("ATS_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")
