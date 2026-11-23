from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from radar.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


BUSY_TIMEOUT_MS = 15_000


def make_engine(url: str = DATABASE_URL, **kwargs):
    if not url.startswith("sqlite"):
        return create_engine(url, **kwargs)
    kwargs.setdefault("connect_args", {"check_same_thread": False})
    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    """WAL: el panel puede leer mientras la tarea diaria escribe. busy_timeout: si la base está
    ocupada (otra escritura, OneDrive sincronizando), espera en vez de fallar al instante."""
    cur = dbapi_conn.cursor()
    try:
        cur.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        cur.execute("PRAGMA journal_mode = WAL")  # en memoria responde 'memory', sin efecto
        cur.execute("PRAGMA synchronous = NORMAL")
    finally:
        cur.close()


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(bind=None) -> None:
    from radar import models  # noqa: F401  (registra las tablas)

    bind = bind or engine
    Base.metadata.create_all(bind=bind)
    _add_missing_columns(bind)


def _add_missing_columns(bind) -> None:
    """Migración mínima: agrega columnas nuevas del modelo a tablas que ya existen.

    Suficiente para este proyecto personal; si el esquema crece, conviene pasar a Alembic.
    """
    insp = inspect(bind)
    with bind.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    col_type = col.type.compile(dialect=bind.dialect)
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col_type}'))
