"""Database engine and session management.

Defaults to SQLite so a fresh clone runs with no external services. Production
(docker-compose) sets DATABASE_URL to Postgres, because the api and worker are
separate containers writing concurrently and SQLite's locking is not dependable
across that boundary.
"""

from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import BASE_DIR, DATABASE_URL


class Base(DeclarativeBase):
    pass


_is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope():
    """Transactional scope. Commits on success, rolls back on exception."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session():
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db():
    """Bring the schema to head.

    Was `create_all`, which creates missing tables but never alters an existing
    one -- so a new column appeared on a fresh database and silently did not on
    a deployed one, and every query touching it failed at runtime with no
    migration to point at.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    # The application owns its logging; alembic.ini's section is for the CLI.
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


def current_revision() -> str | None:
    """The revision this database is on. Surfaced by /health, because "the
    migration did not run" and "the code is old" look identical from outside."""
    from alembic.migration import MigrationContext

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()
