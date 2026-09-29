"""Periodic scheduler. Modules register ticks with @periodic; the scheduler knows nothing
about what they do (e.g. app.services.search enqueues due saved searches)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from app.core.config import get_settings

log = logging.getLogger(__name__)

PERIODIC: list[Callable[[], Any]] = []


def periodic(fn: Callable[[], Any]) -> Callable[[], Any]:
    """Register a sync function run on every scheduler tick (jobs.scheduler_interval_s)."""
    if fn not in PERIODIC:
        PERIODIC.append(fn)
    return fn


def run_periodic_once() -> None:
    for fn in PERIODIC:
        try:
            result = fn()
            if result:
                log.info("%s: %s", fn.__name__, result)
        except Exception:  # noqa: BLE001 - one failing tick must not stop the others
            log.exception("periodic %s failed", fn.__name__)


class Scheduler:
    def __init__(self, interval: float | None = None) -> None:
        self.interval = interval or get_settings().jobs.scheduler_interval_s
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            run_periodic_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
