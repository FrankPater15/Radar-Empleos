from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from radar.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


def make_engine(url: str = DATABASE_URL):
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    return create_engine(url, **kwargs)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(bind=None) -> None:
    from radar import models  # noqa: F401  (registra las tablas)

    Base.metadata.create_all(bind=bind or engine)
