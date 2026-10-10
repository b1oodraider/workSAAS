"""Daily housekeeping: old finished jobs, stale LLM cache, old embeddings and raw source JSON go;
the rest stays. The scheduler only enqueues one job; the worker runs the purges."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import event, select, text

from app.core.db import get_engine, session_scope, utcnow
from app.jobs.queue import drain
from app.jobs.scheduler import PERIODIC
from app.llm import get_gateway
from app.models import Job, JobStatus, LLMCache, LLMUsage, Vacancy
from app.services import maintenance


def _days_ago(days: int):
    return utcnow() - timedelta(days=days)


def test_old_finished_jobs_are_deleted_active_ones_kept(user_id, monkeypatch):
    monkeypatch.setattr(maintenance, "CHUNK", 2)  # several chunks
    with session_scope() as s:
        old = _days_ago(40)
        parent = Job(kind="search_run", status=JobStatus.done, created_at=old, finished_at=old)
        s.add(parent)
        s.flush()
        s.add_all([
            Job(kind="analysis", status=JobStatus.done, created_at=old, finished_at=old, parent_id=parent.id),
            Job(kind="analysis", status=JobStatus.failed, created_at=old, finished_at=None),  # no finish time
            Job(kind="analysis", status=JobStatus.failed, created_at=old, finished_at=old),
            Job(kind="analysis", status=JobStatus.done, created_at=old, finished_at=_days_ago(5)),  # recent
            Job(kind="analysis", status=JobStatus.queued, created_at=old),
            Job(kind="analysis", status=JobStatus.running, created_at=old, started_at=old),
            Job(kind="analysis", status=JobStatus.queued, created_at=old, parent_id=parent.id),
        ])
    assert maintenance.purge_old_jobs() == 4
    with session_scope() as s:
        left = s.scalars(select(Job).order_by(Job.id)).all()
        assert sorted(j.status.value for j in left) == ["done", "queued", "queued", "running"]
        assert all(j.parent_id is None for j in left)  # ON DELETE SET NULL, the child survives
    assert maintenance.purge_old_jobs() == 0


def test_stale_llm_cache_deleted_usage_kept(user_id):
    with session_scope() as s:
        for key, days in (("old", 100), ("fresh", 10)):
            s.add(LLMCache(key=key, task="match", provider="fake", model="m", output={}, created_at=_days_ago(days)))
        s.add(LLMUsage(user_id=user_id, task="match", provider="fake", model="m", created_at=_days_ago(400)))
    assert maintenance.purge_llm_cache() == 1
    with session_scope() as s:
        assert list(s.scalars(select(LLMCache.key))) == ["fresh"]
        assert len(s.scalars(select(LLMUsage)).all()) == 1  # billing history is never purged


def test_raw_json_of_old_vacancies_is_cleared():
    with session_scope() as s:
        s.add_all([
            Vacancy(source="hh", external_id="old", title="t", description="keep me", raw={"big": "json"},
                    fetched_at=_days_ago(31)),
            Vacancy(source="hh", external_id="fresh", title="t", raw={"big": "json"}, fetched_at=_days_ago(1)),
            Vacancy(source="hh", external_id="none", title="t", raw=None, fetched_at=_days_ago(31)),
        ])
    assert maintenance.drop_old_raw() == 2  # a JSON "null" stored by the ORM becomes SQL NULL too
    assert maintenance.drop_old_raw() == 0  # ... once: no endless loop on it
    with session_scope() as s:
        rows = dict(s.execute(text("SELECT external_id, raw FROM vacancies")).all())
        assert rows == {"old": None, "fresh": '{"big": "json"}', "none": None}
        assert s.scalar(select(Vacancy.description).where(Vacancy.external_id == "old")) == "keep me"


def test_purges_page_by_key_not_from_the_start(monkeypatch):
    """Keyset pagination: each chunk starts after the last row of the previous one."""
    monkeypatch.setattr(maintenance, "CHUNK", 2)
    old = _days_ago(40)
    with session_scope() as s:
        s.add_all([Job(kind="analysis", status=JobStatus.done, created_at=old, finished_at=old) for _ in range(5)])
        s.add_all([LLMCache(key=f"k{i}", task="match", provider="fake", model="m", output={},
                            created_at=_days_ago(100)) for i in range(5)])
        s.add_all([Vacancy(source="hh", external_id=str(i), title="t", raw={"a": 1}, fetched_at=old)
                   for i in range(5)])
    statements = []

    def spy(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(get_engine(), "before_cursor_execute", spy)
    try:
        assert maintenance.purge_old_jobs() == 5
        assert maintenance.purge_llm_cache() == 5
        assert maintenance.drop_old_raw() == 5
    finally:
        event.remove(get_engine(), "before_cursor_execute", spy)
    assert len(statements) == 3 * 4  # 2 + 2 + 1 rows, then an empty page: no re-scans
    assert all(">" in sql for sql in statements)


def test_daily_tick_only_enqueues_one_job(monkeypatch):
    monkeypatch.setattr(maintenance, "_last_run", [0.0])
    job_id = maintenance.daily_maintenance()
    assert job_id and maintenance.daily_maintenance() is None  # once a day
    assert maintenance.enqueue_maintenance() == job_id  # already queued: no second job
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.kind == "maintenance" and job.user_id is None and job.status == JobStatus.queued
    assert maintenance.daily_maintenance in PERIODIC


async def test_maintenance_job_runs_every_purge(user_id):
    with session_scope() as s:
        s.add(LLMCache(key="old", task="match", provider="fake", model="m", output={}, created_at=_days_ago(100)))
    job_id = maintenance.enqueue_maintenance()
    assert await drain() == 1
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.done and job.result == {"llm_cache_deleted": 1}
    assert maintenance.enqueue_maintenance() != job_id  # the finished one does not block the next


async def test_one_failing_purge_does_not_stop_the_others(monkeypatch):
    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(maintenance, "PURGES", {"jobs_deleted": broken, "llm_cache_deleted": lambda: 2,
                                                "embeddings_deleted": lambda: 0})
    assert await maintenance.run_maintenance() == {"llm_cache_deleted": 2}


async def test_embeddings_purge_is_part_of_maintenance(monkeypatch):
    monkeypatch.setattr(get_gateway(), "purge_embeddings", lambda: 5)
    assert (await maintenance.run_maintenance())["embeddings_deleted"] == 5


async def test_run_maintenance_reports_only_changes(user_id):
    assert await maintenance.run_maintenance() == {}
    with session_scope() as s:
        s.add(LLMCache(key="old", task="match", provider="fake", model="m", output={}, created_at=_days_ago(100)))
    assert await maintenance.run_maintenance() == {"llm_cache_deleted": 1}


def test_once_a_day_guard(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(maintenance.time, "monotonic", lambda: now[0])
    state = [0.0]
    assert maintenance.once_a_day(state) is True
    now[0] += 23 * 3600
    assert maintenance.once_a_day(state) is False
    now[0] += 2 * 3600
    assert maintenance.once_a_day(state) is True
