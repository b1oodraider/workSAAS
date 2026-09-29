"""DB-backed job queue with an in-process asyncio worker.

Handlers are registered with ``@job_handler("kind")`` and receive a JobContext.
They return a small JSON-able dict (stored in ``jobs.result``) or None; a
``result_url`` key in it tells the UI where to send the user when the job is done.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.db import session_scope, utcnow
from app.llm.base import LLMError
from app.models import Job, JobStatus

log = logging.getLogger(__name__)


@dataclass
class JobContext:
    job_id: int
    user_id: int | None
    payload: dict[str, Any]
    attempt: int = 1

    @property
    def final_attempt(self) -> bool:
        return self.attempt >= get_settings().jobs.max_attempts

    def enqueue_child(self, kind: str, payload: dict[str, Any], *, title: str = "",
                      result_url: str | None = None) -> int:
        return enqueue(kind, payload, user_id=self.user_id, title=title,
                       result_url=result_url, parent_id=self.job_id)


Handler = Callable[[JobContext], Awaitable[dict[str, Any] | None]]
_HANDLERS: dict[str, Handler] = {}


class JobError(Exception):
    """Expected, user-facing job failure (message is shown in the UI)."""


def job_handler(kind: str) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        if kind in _HANDLERS and _HANDLERS[kind] is not fn:
            raise RuntimeError(f"Duplicate job handler for {kind!r}")
        _HANDLERS[kind] = fn
        return fn

    return decorator


def registered_kinds() -> list[str]:
    return sorted(_HANDLERS)


def enqueue(
    kind: str,
    payload: dict[str, Any],
    *,
    user_id: int | None,
    title: str = "",
    result_url: str | None = None,
    parent_id: int | None = None,
) -> int:
    if kind not in _HANDLERS:
        raise RuntimeError(f"No handler registered for job kind {kind!r}")
    with session_scope() as s:
        job = Job(kind=kind, payload=payload, user_id=user_id, title=title,
                  result_url=result_url, parent_id=parent_id)
        s.add(job)
        s.flush()
        return job.id


def recover_stale_jobs() -> int:
    """Jobs left 'running' by a stopped process go back to the queue — unless they have
    used up their attempts (a job that crashes the process must not loop forever)."""
    max_attempts = get_settings().jobs.max_attempts
    with session_scope() as s:
        s.execute(
            update(Job).where(Job.status == JobStatus.running, Job.attempts >= max_attempts)
            .values(status=JobStatus.failed, finished_at=utcnow(),
                    error="Задача прервалась (перезапуск сервера) и больше не повторяется")
        )
        res = s.execute(
            update(Job).where(Job.status == JobStatus.running).values(status=JobStatus.queued)
        )
        return res.rowcount or 0


def _claim_next() -> Job | None:
    with session_scope() as s:
        job_id = s.scalar(
            select(Job.id).where(Job.status == JobStatus.queued).order_by(Job.id).limit(1)
        )
        if job_id is None:
            return None
        res = s.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.queued)
            .values(status=JobStatus.running, started_at=utcnow(), attempts=Job.attempts + 1)
        )
        if not res.rowcount:
            return None
        return s.get(Job, job_id)


async def run_job(job: Job) -> None:
    handler = _HANDLERS.get(job.kind)
    ctx = JobContext(job_id=job.id, user_id=job.user_id, payload=dict(job.payload or {}),
                     attempt=job.attempts)
    status, error, result = JobStatus.done, None, None
    try:
        if handler is None:
            raise JobError(f"Нет обработчика для задачи {job.kind!r}")
        result = await handler(ctx)
    except (JobError, LLMError) as exc:
        retryable = getattr(exc, "retryable", False)
        if retryable and job.attempts < get_settings().jobs.max_attempts:
            status = JobStatus.queued
        else:
            status = JobStatus.failed
        error = str(exc)
        log.warning("job %s (%s) failed: %s", job.id, job.kind, exc)
    except Exception as exc:  # noqa: BLE001 - worker must survive any handler bug
        status, error = JobStatus.failed, ("Внутренняя ошибка. Попробуйте ещё раз; если повторится — "
                                           f"напишите администратору (код: {type(exc).__name__})")
        log.error("job %s (%s) crashed:\n%s", job.id, job.kind, traceback.format_exc())
    with session_scope() as s:
        values: dict[str, Any] = {"status": status, "error": error}
        if status != JobStatus.queued:
            values["finished_at"] = utcnow()
        if result is not None:
            values["result"] = result
            if result.get("result_url"):
                values["result_url"] = result["result_url"]
        s.execute(update(Job).where(Job.id == job.id).values(**values))


async def drain(max_jobs: int = 1000) -> int:
    """Run queued jobs sequentially until the queue is empty. Used by tests and CLI."""
    count = 0
    while count < max_jobs:
        job = _claim_next()
        if job is None:
            return count
        await run_job(job)
        count += 1
    return count


class Worker:
    def __init__(self, concurrency: int | None = None, poll_interval: float | None = None) -> None:
        cfg = get_settings().jobs
        self.concurrency = concurrency or cfg.concurrency
        self.poll_interval = poll_interval or cfg.poll_interval_s
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        recovered = recover_stale_jobs()
        if recovered:
            log.info("requeued %d stale jobs", recovered)
        sem = asyncio.Semaphore(self.concurrency)
        while not self._stop.is_set():
            await sem.acquire()
            job = _claim_next()
            if job is None:
                sem.release()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            task = asyncio.create_task(run_job(job))
            self._tasks.add(task)
            task.add_done_callback(lambda t: (self._tasks.discard(t), sem.release()))
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()
