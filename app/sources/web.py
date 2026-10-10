"""Page fetching for scraping sources: polite HTTP client with a real-browser fallback.

Sources that have no usable public API (hh.ru without a key, Habr Career pages,
Telegram channel previews) fetch HTML through a ``PageFetcher``:

- ``HttpFetcher``   — httpx with browser-like headers, cookie jar and per-host rate limit;
- ``BrowserFetcher``— headless Chromium via Playwright (optional: ``pip install -e ".[browser]"``
                       and ``playwright install chromium``), for sites behind JS anti-bot checks;
- ``AutoFetcher``   — HTTP first, switches to the browser when a page looks blocked.

Every fetcher (and ``fetch_json``) reads at most ``MAX_RESPONSE_BYTES`` and respects the
process-wide per-host circuit breaker: after a 429/439 or an anti-bot page the host is left
alone for an hour (doubling up to 6 h) instead of being hit again by every query of a run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings
from app.core.http import chromium_launch_options, chromium_slot
from app.sources.base import SourceError

log = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

FetchMode = Literal["http", "browser", "auto"]
FETCH_MODES = ("http", "browser", "auto")

# Markers of anti-bot / captcha pages (lower-cased substrings).
BLOCK_MARKERS = ("captcha", "ddos-guard", "cf-challenge", "access denied", "доступ ограничен")
# Rate-limit statuses that open the circuit breaker (439: Avito's "проверка безопасности").
BLOCK_STATUSES = (429, 439)
# Hard cap on one response body: a misbehaving site must not eat the worker's memory.
MAX_RESPONSE_BYTES = 15 * 1024 * 1024


@dataclass
class Page:
    url: str
    status: int
    text: str

    @property
    def looks_blocked(self) -> bool:
        if self.status in (401, 403, 429, 503) or self.status in BLOCK_STATUSES:
            return True
        if "captcha" in urlsplit(self.url).path.lower():  # not the query: a search for "captcha qa"
            return True
        head = self.text[:5000].lower()
        return len(self.text) < 20000 and any(m in head for m in BLOCK_MARKERS)


class PageFetcher(Protocol):
    async def get(self, url: str, params: dict[str, Any] | None = None) -> Page: ...

    async def close(self) -> None: ...


class _HostRateLimiter:
    """Minimum delay between requests to the same host (process-wide), with jitter."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str, min_interval: float) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            delay = self._last.get(host, 0.0) + min_interval - now
            if delay > 0:
                await asyncio.sleep(delay + random.uniform(0, min_interval * 0.3))
            self._last[host] = time.monotonic()


rate_limiter = _HostRateLimiter()


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _site(host: str) -> str:
    return host.removeprefix("www.") or host


class _HostBreaker:
    """Process-wide circuit breaker: a host that rate-limited us or showed an anti-bot page is
    not contacted again until the pause ends (1 h, doubling on every new block up to 6 h)."""

    BASE_S = 3600.0
    MAX_S = 6 * 3600.0

    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._backoff: dict[str, float] = {}

    def check(self, host: str) -> None:
        """Raise SourceError, without any network call, while ``host`` is paused."""
        until = self._until.get(host)
        if until is None:
            return
        if until <= time.time():
            del self._until[host]
            return
        minutes = max(1, round((until - time.time()) / 60))
        raise SourceError(f"{_site(host)} временно ограничил доступ — пауза ещё ~{minutes} мин")

    def trip(self, host: str, seconds: float | None = None) -> None:
        """Pause ``host``: for ``seconds`` (e.g. the server's Retry-After) or the growing backoff."""
        if self.blocked(host):
            return  # requests already in flight when the block hit must not stack the pause
        if seconds is not None:
            backoff = min(self.MAX_S, max(60.0, seconds))
        else:
            backoff = min(self.MAX_S, self._backoff.get(host, self.BASE_S / 2) * 2)
            self._backoff[host] = backoff
        self._until[host] = time.time() + backoff
        log.warning("%s blocked us; no requests to it for %.0f min", host, backoff / 60)

    def ok(self, host: str) -> None:
        """A normal answer: the next block starts from the base pause again."""
        if host not in self._until:
            self._backoff.pop(host, None)

    def blocked(self, host: str) -> bool:
        return self._until.get(host, 0.0) > time.time()

    def clear(self) -> None:
        self._until.clear()
        self._backoff.clear()


breaker = _HostBreaker()


def _is_block(page: Page) -> bool:
    """Pause-worthy: rate limiting, a captcha page, or an anti-bot marker on an error page.
    A bare 401/403/503 or a normal page that mentions "captcha" is not (it would pause the site
    for every user); `looks_blocked` stays broader — it only decides switching to the browser."""
    if page.status in BLOCK_STATUSES or "captcha" in urlsplit(page.url).path.lower():
        return True
    head = page.text[:5000].lower()
    if page.status != 200:
        return any(m in head for m in BLOCK_MARKERS)
    # A 200 anti-bot stub is tiny and says so up front; a normal page merely mentioning a captcha isn't.
    return len(page.text) < 20000 and any(m in head[:1000] for m in BLOCK_MARKERS)


def _note_page(host: str, page: Page) -> None:
    if _is_block(page):
        breaker.trip(host)
    else:
        breaker.ok(host)


async def _read_capped(resp: httpx.Response, host: str) -> bytes:
    length = resp.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
        raise SourceError(f"{_site(host)}: ответ слишком большой")
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise SourceError(f"{_site(host)}: ответ слишком большой")
        chunks.append(chunk)
    return b"".join(chunks)


class _TTLCache:
    """Tiny in-process cache: one search run asks the same feed for every query."""

    def __init__(self, max_items: int = 256) -> None:
        self._data: dict[Any, tuple[float, Any]] = {}
        self.max_items = max_items

    def get(self, key: Any) -> Any | None:
        item = self._data.get(key)
        if item is None or item[0] < time.monotonic():
            self._data.pop(key, None)
            return None
        return item[1]

    def put(self, key: Any, value: Any, ttl: float) -> None:
        if len(self._data) >= self.max_items:
            self._data.pop(next(iter(self._data)))
        self._data[key] = (time.monotonic() + ttl, value)

    def clear(self) -> None:
        self._data.clear()


response_cache = _TTLCache()


def _cache_key(url: str, params: dict[str, Any] | None) -> tuple:
    return (url, tuple(sorted((params or {}).items())))


class HttpFetcher:
    def __init__(self, *, use_proxy: bool = False, min_interval: float = 1.5,
                 headers: dict[str, str] | None = None, timeout: float = 30.0,
                 cache_ttl: float = 0.0, trip_breaker: bool = True) -> None:
        proxy = get_settings().proxy_url if use_proxy else None
        self.min_interval = min_interval
        self.cache_ttl = cache_ttl
        # AutoFetcher turns this off: there a blocked page first means "switch to the browser".
        self.trip_breaker = trip_breaker
        self.client = httpx.AsyncClient(proxy=proxy, timeout=timeout, follow_redirects=True,
                                        headers={**BROWSER_HEADERS, **(headers or {})})

    async def get(self, url: str, params: dict[str, Any] | None = None) -> Page:
        key = _cache_key(url, params)
        if self.cache_ttl and (cached := response_cache.get(key)) is not None:
            return cached
        host = _host(url)
        breaker.check(host)
        await rate_limiter.wait(host, self.min_interval)
        try:
            async with self.client.stream("GET", url, params=params) as resp:
                body = await _read_capped(resp, host)
                page = Page(url=str(resp.url), status=resp.status_code,
                            text=body.decode(resp.encoding or "utf-8", errors="replace"))
        except httpx.HTTPError as exc:
            raise SourceError(f"{_site(host)} недоступен: {exc}") from exc
        if self.trip_breaker:
            _note_page(host, page)
        if self.cache_ttl and page.status == 200 and not page.looks_blocked:
            response_cache.put(key, page, self.cache_ttl)
        return page

    async def close(self) -> None:
        await self.client.aclose()


class BrowserFetcher:
    """Headless Chromium. One browser per fetcher; pages are opened sequentially."""

    def __init__(self, *, use_proxy: bool = False, min_interval: float = 2.0,
                 timeout: float = 45.0) -> None:
        self.use_proxy = use_proxy
        self.min_interval = min_interval
        self.timeout_ms = int(timeout * 1000)
        self._pw = None
        self._browser = None
        self._context = None
        self._holds_slot = False

    @staticmethod
    def available() -> bool:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False
        return True

    async def _ensure(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SourceError(
                "Браузерный режим не установлен: pip install -e \".[browser]\" && playwright install chromium"
            ) from exc
        launch = chromium_launch_options(use_proxy=self.use_proxy)
        # Held until close(): waits while the applier (or another fetcher) has a Chromium running.
        await chromium_slot.acquire()
        self._holds_slot = True
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(**launch)
            self._context = await self._browser.new_context(
                user_agent=BROWSER_HEADERS["User-Agent"], locale="ru-RU",
                viewport={"width": 1366, "height": 900},
            )
        except Exception as exc:  # noqa: BLE001 - missing browser binary etc.
            await self.close()
            raise SourceError(f"Не удалось запустить браузер: {exc}. Выполните: playwright install chromium") from exc

    async def get(self, url: str, params: dict[str, Any] | None = None) -> Page:
        if params:
            url = str(httpx.URL(url, params=params))
        host = _host(url)
        breaker.check(host)
        await self._ensure()
        await rate_limiter.wait(host, self.min_interval)
        tab = None
        try:
            tab = await self._context.new_page()
            resp = await tab.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            # Give anti-bot JS a moment to redirect to the real page.
            await tab.wait_for_timeout(1500)
            text = await tab.content()
            page = Page(url=tab.url, status=resp.status if resp else 0, text=text)
        except Exception as exc:  # noqa: BLE001 - playwright timeout/network errors
            raise SourceError(f"Браузер не смог открыть {url}: {exc}") from exc
        finally:
            if tab is not None:
                await tab.close()
        if len(page.text) > MAX_RESPONSE_BYTES:  # chars >= bytes / 4; the page is already in memory
            raise SourceError(f"{_site(host)}: ответ слишком большой")
        _note_page(host, page)
        return page

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            try:
                if self._pw is not None:
                    await self._pw.stop()
            finally:
                self._pw = self._browser = self._context = None
                if self._holds_slot:
                    self._holds_slot = False
                    chromium_slot.release()


class AutoFetcher:
    """HTTP first; after the first blocked page, use the browser for the rest of the run.
    The circuit breaker opens only when the browser is blocked too (or is not installed)."""

    def __init__(self, *, use_proxy: bool = False, min_interval: float = 1.5, cache_ttl: float = 0.0) -> None:
        self.http = HttpFetcher(use_proxy=use_proxy, min_interval=min_interval, cache_ttl=cache_ttl,
                                trip_breaker=False)
        self.browser = BrowserFetcher(use_proxy=use_proxy, min_interval=min_interval)
        self._use_browser = False

    async def get(self, url: str, params: dict[str, Any] | None = None) -> Page:
        if not self._use_browser:
            page = await self.http.get(url, params)
            if not page.looks_blocked:
                breaker.ok(_host(url))
                return page
            if not BrowserFetcher.available():
                if _is_block(page):
                    breaker.trip(_host(url))
                return page
            log.info("%s looks blocked for plain HTTP, switching to browser", _host(url))
            self._use_browser = True
        return await self.browser.get(url, params)

    async def close(self) -> None:
        try:
            await self.http.close()
        finally:
            await self.browser.close()


async def fetch_json(url: str, *, params: dict[str, Any] | None = None, use_proxy: bool = False,
                     headers: dict[str, str] | None = None, source: str = "",
                     min_interval: float = 0.5, cache_ttl: float = 0.0) -> Any:
    """GET a JSON API with the shared rate limiter and circuit breaker; errors become SourceError."""
    key = ("json",) + _cache_key(url, params)
    if cache_ttl and (cached := response_cache.get(key)) is not None:
        return cached
    host = _host(url)
    name = source or _site(host)
    breaker.check(host)
    await rate_limiter.wait(host, min_interval)
    proxy = get_settings().proxy_url if use_proxy else None
    async with httpx.AsyncClient(proxy=proxy, timeout=30.0, follow_redirects=True,
                                 headers={**BROWSER_HEADERS, "Accept": "application/json",
                                          **(headers or {})}) as client:
        try:
            async with client.stream("GET", url, params=params) as resp:
                status = resp.status_code
                retry_after = resp.headers.get("retry-after", "")
                body = await _read_capped(resp, host) if status < 400 else b""
        except httpx.HTTPError as exc:
            raise SourceError(f"{name} недоступен: {exc}") from exc
    if status in BLOCK_STATUSES:
        # APIs say how long to wait: a per-minute limit must not switch the source off for an hour.
        breaker.trip(host, float(retry_after) if retry_after.isdigit() else None)
        raise SourceError(f"{name} ограничил частоту запросов ({status}), попробуйте позже")
    if status >= 400:
        raise SourceError(f"{name} ответил {status}")
    breaker.ok(host)
    try:
        data = json.loads(body)
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise SourceError(f"{name} вернул не JSON") from exc
    if cache_ttl:
        response_cache.put(key, data, cache_ttl)
    return data


def make_fetcher(mode: FetchMode | str = "auto", *, use_proxy: bool = False,
                 min_interval: float = 1.5, cache_ttl: float = 0.0) -> PageFetcher:
    if mode not in FETCH_MODES:
        # A typo in config must not silently escalate to a headless browser.
        log.warning("unknown fetcher %r, using plain HTTP", mode)
        mode = "http"
    if mode == "http":
        return HttpFetcher(use_proxy=use_proxy, min_interval=min_interval, cache_ttl=cache_ttl)
    if mode == "browser":
        return BrowserFetcher(use_proxy=use_proxy, min_interval=min_interval)
    return AutoFetcher(use_proxy=use_proxy, min_interval=min_interval, cache_ttl=cache_ttl)
