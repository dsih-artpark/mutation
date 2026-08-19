"""The migration chain must describe exactly what the models declare.

A baseline that drifts from the models corrupts the first upgrade that runs
against a real database, and nothing before that point reveals it.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.db import Base

ALEMBIC_INI = "alembic.ini"


def _config(url: str) -> Config:
    config = Config(ALEMBIC_INI)
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture()
def migrated_engine(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrate.db'}"
    command.upgrade(_config(url), "head")
    return create_engine(url)


def test_the_chain_produces_exactly_what_the_models_declare(migrated_engine):
    import app.models  # noqa: F401  registers the mappers

    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)

    assert diff == [], f"models and migrations disagree: {diff}"


def test_every_revision_downgrades(tmp_path):
    """A revision that cannot be reversed is a deploy with no way back."""
    url = f"sqlite:///{tmp_path / 'down.db'}"
    config = _config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")


def test_the_backfill_gives_every_historical_run_a_unique_external_id(tmp_path):
    """external_id is NOT NULL and unique, and historical rows have none.

    Backfilled with the run's own id: unique by construction, stable, and
    correct in the only sense that matters -- nothing will ever resubmit it.
    """
    from sqlalchemy import text

    url = f"sqlite:///{tmp_path / 'backfill.db'}"
    config = _config(url)

    command.upgrade(config, "0001")
    engine = create_engine(url)
    with engine.begin() as connection:
        for run_id in ("run-a", "run-b"):
            connection.execute(text(
                "INSERT INTO runs (id, pipeline_id, pipeline_version, status, "
                "params, metrics, artifacts_purged, created_at) VALUES "
                f"('{run_id}', 'p', '1', 'SUCCEEDED', '{{}}', '{{}}', 0, "
                "'2026-01-01 00:00:00')"
            ))

    command.upgrade(config, "head")

    with engine.connect() as connection:
        rows = dict(connection.execute(text(
            "SELECT id, external_id FROM runs"
        )).all())

    assert rows == {"run-a": "run-a", "run-b": "run-b"}
