from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from docket.models.base import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def configure_database(database_url: str) -> Engine:
    global _engine, _session_factory
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    _engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, class_=Session)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database has not been configured")
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        raise RuntimeError("Database has not been configured")
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_schema_for_smoke() -> None:
    Base.metadata.create_all(get_engine())
