"""Shared outbound HTTP client factory (httpx) with optional proxy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from app.core.config import get_settings

USER_AGENT = "worksaas/0.1 (self-hosted job search assistant)"


def make_async_client(
    *,
    use_proxy: bool = False,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
) -> httpx.AsyncClient:
    settings = get_settings()
    proxy = settings.proxy_url if use_proxy else None
    return httpx.AsyncClient(
        proxy=proxy,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
        follow_redirects=follow_redirects,
    )


def playwright_proxy(use_proxy: bool) -> dict[str, str] | None:
    """Proxy settings for Playwright's ``launch(proxy=...)``.

    Chromium ignores credentials embedded in the proxy URL, so they are passed separately.
    """
    url = get_settings().proxy_url if use_proxy else ""
    if not url:
        return None
    parts = urlsplit(url)
    server = f"{parts.scheme or 'http'}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    proxy = {"server": server}
    if parts.username:
        proxy["username"] = unquote(parts.username)
        proxy["password"] = unquote(parts.password or "")
    return proxy


# No background calls to Google services from automation browsers.
QUIET_CHROMIUM_ARGS = ("--disable-background-networking", "--disable-component-update", "--no-first-run",
                       "--disable-sync", "--metrics-recording-only", "--disable-default-apps")


def chromium_launch_options(*, headless: bool = True, use_proxy: bool = False,
                            executable: str = "") -> dict[str, Any]:
    """Keyword arguments for Playwright's ``chromium.launch`` shared by fetchers and appliers."""
    launch: dict[str, Any] = {"headless": headless, "args": list(QUIET_CHROMIUM_ARGS)}
    if executable:
        launch["executable_path"] = executable
    proxy = playwright_proxy(use_proxy)
    if proxy:
        launch["proxy"] = proxy
    return launch


class ChromiumSlot:
    """One automation Chromium per process: two at once can exhaust a small (512 MB) container.

    Held for the browser's whole lifetime by the applier and by ``BrowserFetcher``. Re-entrant per
    task, so code that already holds the slot and then starts a fetcher in the same task proceeds
    instead of waiting for itself (a deadlock)."""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._owner: asyncio.Task | None = None
        self._depth = 0

    def _lock_for_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:  # a new event loop (tests, restarts)
            self._lock, self._loop, self._owner, self._depth = asyncio.Lock(), loop, None, 0
        return self._lock

    async def acquire(self) -> None:
        lock = self._lock_for_loop()
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            self._depth += 1
            return
        await lock.acquire()
        self._owner, self._depth = task, 1

    def release(self) -> None:
        if self._lock is None or self._depth <= 0:
            return
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    @property
    def busy(self) -> bool:
        try:
            if asyncio.get_running_loop() is not self._loop:
                return False  # held in a loop that is gone: _lock_for_loop() starts afresh
        except RuntimeError:
            pass
        return self._depth > 0

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        await self.acquire()
        try:
            yield
        finally:
            self.release()


chromium_slot = ChromiumSlot()
