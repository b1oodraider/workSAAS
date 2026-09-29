from __future__ import annotations

import sqlite3

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from app.cli import ROOT, _migrate
from app.core.db import Base, get_engine, init_engine


def test_migrations_match_models(tmp_path):
    init_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    _migrate()
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"Models changed without a migration: {diff}"


def test_upgrade_preserves_user_data(tmp_path):
    """Batch migrations rebuild tables; with foreign keys ON that used to cascade-delete data."""
    path = tmp_path / "old.db"
    init_engine(f"sqlite:///{path}")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "0002")

    db = sqlite3.connect(path)
    db.executescript("""
        INSERT INTO users(id, username, password_hash, is_admin, is_active, created_at)
            VALUES (1, 'u', 'x', 0, 1, '2026-01-01');
        INSERT INTO resumes(id, user_id, title, text, preferences, created_at, updated_at)
            VALUES (1, 1, 'CV', 'text', '', '2026-01-01', '2026-01-01');
        INSERT INTO vacancies(id, source, external_id, title, skills, description, is_partial, fetched_at, created_at)
            VALUES (1, 'hh', '1', 'Dev', '[]', '', 0, '2026-01-01', '2026-01-01');
        INSERT INTO user_vacancies(user_id, vacancy_id, status, notes, created_at)
            VALUES (1, 1, 'applied', '', '2026-01-01');
        INSERT INTO llm_usage(user_id, task, provider, model, input_tokens, output_tokens, cache_read_tokens,
                              cache_write_tokens, cost_usd, cached, ok, latency_ms, created_at)
            VALUES (1, 'match', 'a', 'm', 1, 1, 0, 0, 0.1, 0, 1, 1, '2026-01-01');
    """)
    db.commit()
    db.close()

    command.upgrade(cfg, "head")

    db = sqlite3.connect(path)
    counts = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("users", "resumes", "vacancies", "user_vacancies", "llm_usage")}
    assert counts == {"users": 1, "resumes": 1, "vacancies": 1, "user_vacancies": 1, "llm_usage": 1}
    assert db.execute("SELECT status_history FROM user_vacancies").fetchone()[0] == "[]"
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
