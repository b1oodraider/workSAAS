from __future__ import annotations

from alembic import context

import app.models  # noqa: F401  - populate metadata
from app.core.db import Base, get_engine

target_metadata = Base.metadata


def run_migrations() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            # Batch migrations rebuild tables (create copy, drop original, rename). With
            # foreign keys ON, dropping e.g. "users" would cascade-delete every resume.
            # The pragma only works outside a transaction, so it goes first.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=sqlite,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()
        if sqlite:
            broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            if broken:
                raise RuntimeError(f"Foreign key violations after migration: {broken[:5]}")


run_migrations()
