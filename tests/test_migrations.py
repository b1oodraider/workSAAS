from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.cli import _migrate
from app.core.db import Base, get_engine, init_engine


def test_migrations_match_models(tmp_path):
    init_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    _migrate()
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"Models changed without a migration: {diff}"
