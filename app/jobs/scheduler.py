"""Periodic scheduler: enqueues search runs for saved searches that are due."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import session_scope, utcnow
from app.models import Job, JobStatus, SavedSearch

log = logging.getLogger(__name__)


def enqueue_due_searches() -> list[int]:
    from app.services.search import enqueue_search_run  # avoid import cycle at module load

    now = utcnow()
    due: list[tuple[int, int]] = []
    with session_scope() as s:
        searches = s.scalars(
            select(SavedSearch).where(SavedSearch.enabled.is_(True), SavedSearch.interval_minutes > 0)
        ).all()
        active = {
            (j.payload or {}).get("search_id")
            for j in s.scalars(
                select(Job).where(
                    Job.kind == "search_run", Job.status.in_([JobStatus.queued, JobStatus.running])
                )
            )
        }
        for search in searches:
            if search.id in active:
                continue
            if search.last_run_at and search.last_run_at + timedelta(minutes=search.interval_minutes) > now:
                continue
            due.append((search.id, search.user_id))
    return [enqueue_search_run(search_id, user_id) for search_id, user_id in due]


class Scheduler:
    def __init__(self, interval: float | None = None) -> None:
        self.interval = interval or get_settings().jobs.scheduler_interval_s
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                ids = enqueue_due_searches()
                if ids:
                    log.info("scheduled search runs: %s", ids)
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
