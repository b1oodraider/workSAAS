from __future__ import annotations

from alembic import context

import app.models  # noqa: F401  - populate metadata
from app.core.db import Base, get_engine

target_metadata = Base.metadata


def run_migrations() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


run_migrations()
