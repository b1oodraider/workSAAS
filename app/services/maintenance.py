"""Daily housekeeping of tables that would otherwise only grow: finished jobs, the LLM answer
cache, stored embeddings and the raw source JSON of old vacancies. ``llm_usage`` is billing
history and is kept.

The scheduler tick only enqueues a ``maintenance`` job; the purges run in the worker, each in a
thread, so the event loop (web requests, the bot) is never blocked by a long cleanup."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from sqlalchemy import delete, func, null, select, update

from app.core.db import session_scope, utcnow
from app.jobs.queue import JobContext, enqueue, job_handler
from app.jobs.scheduler import periodic
from app.llm import get_gateway
from app.models import Job, JobStatus, LLMCache, Vacancy

log = logging.getLogger(__name__)

DAY_S = 24 * 3600
JOBS_KEEP = timedelta(days=30)
LLM_CACHE_KEEP = timedelta(days=90)
RAW_KEEP = timedelta(days=30)
# Rows per statement/transaction: keeps SQLite write locks short for the web app and the worker.
CHUNK = 500
KIND = "maintenance"


def once_a_day(last_run: list[float]) -> bool:
    """True at most once a day per process (the scheduler ticks every minute); ``last_run`` is
    the caller's module-level ``[0.0]`` state."""
    now = time.monotonic()
    if last_run[0] and now - last_run[0] < DAY_S:
        return False
    last_run[0] = now
    return True


def purge_old_jobs(keep: timedelta = JOBS_KEEP) -> int:
    """Delete done/failed jobs finished more than ``keep`` ago; queued/running ones are never touched."""
    cutoff = utcnow() - keep
    finished = (Job.status.in_([JobStatus.done, JobStatus.failed]),
                func.coalesce(Job.finished_at, Job.created_at) < cutoff)
    total, last_id = 0, 0
    while True:
        with session_scope() as s:
            # Keyset pagination: each chunk starts after the previous one instead of re-scanning.
            ids = list(s.scalars(select(Job.id).where(Job.id > last_id, *finished).order_by(Job.id).limit(CHUNK)))
            if not ids:
                return total
            # Children of a deleted job keep living: jobs.parent_id is ON DELETE SET NULL.
            s.execute(delete(Job).where(Job.id.in_(ids)).execution_options(synchronize_session=False))
        total, last_id = total + len(ids), ids[-1]


def purge_llm_cache(keep: timedelta = LLM_CACHE_KEEP) -> int:
    """Delete cached LLM answers stored more than ``keep`` ago (a repeat call simply pays again)."""
    cutoff = utcnow() - keep
    total, last_key = 0, ""
    while True:
        with session_scope() as s:
            keys = list(s.scalars(select(LLMCache.key).where(LLMCache.key > last_key, LLMCache.created_at < cutoff)
                                  .order_by(LLMCache.key).limit(CHUNK)))
            if not keys:
                return total
            s.execute(delete(LLMCache).where(LLMCache.key.in_(keys)).execution_options(synchronize_session=False))
        total, last_key = total + len(keys), keys[-1]


def drop_old_raw(keep: timedelta = RAW_KEEP) -> int:
    """Clear the source's raw JSON of vacancies not fetched again for ``keep``: nothing reads it
    after the search run, and a later fetch stores a fresh copy."""
    cutoff = utcnow() - keep
    total, last_id = 0, 0
    while True:
        with session_scope() as s:
            # IS NOT NULL also catches a JSON "null" stored by older code: it becomes SQL NULL once.
            ids = list(s.scalars(select(Vacancy.id)
                                 .where(Vacancy.id > last_id, Vacancy.fetched_at < cutoff, Vacancy.raw.is_not(None))
                                 .order_by(Vacancy.id).limit(CHUNK)))
            if not ids:
                return total
            s.execute(update(Vacancy).where(Vacancy.id.in_(ids)).values(raw=null())
                      .execution_options(synchronize_session=False))
        total, last_id = total + len(ids), ids[-1]


def purge_embeddings() -> int:
    """Drop vectors stored more than [llm.embeddings] keep_days ago."""
    return get_gateway().purge_embeddings()


PURGES = {"jobs_deleted": purge_old_jobs, "llm_cache_deleted": purge_llm_cache,
          "embeddings_deleted": purge_embeddings, "vacancy_raw_cleared": drop_old_raw}


async def run_maintenance() -> dict[str, int]:
    """All cleanups now, each in a worker thread; returns what changed (row counts) for the log.
    One failing cleanup does not stop the others."""
    counts: dict[str, int] = {}
    for name, purge in PURGES.items():
        try:
            counts[name] = await asyncio.to_thread(purge)
        except Exception:  # noqa: BLE001 - e.g. "database is locked": tomorrow's run tries again
            log.exception("maintenance: %s failed", purge.__name__)
    return {k: v for k, v in counts.items() if v}


@job_handler(KIND)
async def maintenance_job(ctx: JobContext) -> dict[str, int]:
    counts = await run_maintenance()
    if counts:
        log.info("maintenance: %s", counts)
    return counts


def enqueue_maintenance() -> int:
    """A system job (no owner), or the one already waiting/running."""
    with session_scope() as s:
        active = s.scalar(select(Job.id).where(Job.kind == KIND, Job.status.in_([JobStatus.queued, JobStatus.running]))
                          .order_by(Job.id).limit(1))
        if active is not None:
            return active
    return enqueue(KIND, {}, user_id=None, title="Обслуживание базы")


_last_run: list[float] = [0.0]


@periodic
def daily_maintenance() -> int | None:
    """Scheduler tick: once a day, enqueue the cleanup job (it runs in the worker)."""
    if not once_a_day(_last_run):
        return None
    return enqueue_maintenance()
