"""Page fetching for scraping sources: polite HTTP client with a real-browser fallback.

Sources that have no usable public API (hh.ru without a key, Habr Career pages,
Telegram channel previews) fetch HTML through a ``PageFetcher``:

- ``HttpFetcher``   — httpx with browser-like headers, cookie jar and per-host rate limit;
- ``BrowserFetcher``— headless Chromium via Playwright (optional: ``pip install -e ".[browser]"``
                       and ``playwright install chromium``), for sites behind JS anti-bot checks;
- ``AutoFetcher``   — HTTP first, switches to the browser when a page looks blocked.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings
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

# Markers of anti-bot / captcha pages (lower-cased substrings).
BLOCK_MARKERS = ("captcha", "ddos-guard", "cf-challenge", "access denied", "доступ ограничен")


@dataclass
class Page:
    url: str
    status: int
    text: str

    @property
    def looks_blocked(self) -> bool:
        if self.status in (401, 403, 429, 503):
            return True
        if "captcha" in self.url.lower():
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
                 cache_ttl: float = 0.0) -> None:
        proxy = get_settings().proxy_url if use_proxy else None
        self.min_interval = min_interval
        self.cache_ttl = cache_ttl
        self.client = httpx.AsyncClient(proxy=proxy, timeout=timeout, follow_redirects=True,
                                        headers={**BROWSER_HEADERS, **(headers or {})})

    async def get(self, url: str, params: dict[str, Any] | None = None) -> Page:
        key = _cache_key(url, params)
        if self.cache_ttl and (cached := response_cache.get(key)) is not None:
            return cached
        await rate_limiter.wait(urlsplit(url).netloc, self.min_interval)
        try:
            resp = await self.client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise SourceError(f"{urlsplit(url).netloc} недоступен: {exc}") from exc
        page = Page(url=str(resp.url), status=resp.status_code, text=resp.text)
        if self.cache_ttl and page.status == 200:
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
        launch: dict[str, Any] = {"headless": True}
        proxy = get_settings().proxy_url
        if self.use_proxy and proxy:
            launch["proxy"] = {"server": proxy}
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
        await self._ensure()
        if params:
            url = str(httpx.URL(url, params=params))
        await rate_limiter.wait(urlsplit(url).netloc, self.min_interval)
        page = None
        try:
            page = await self._context.new_page()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            # Give anti-bot JS a moment to redirect to the real page.
            await page.wait_for_timeout(1500)
            text = await page.content()
            return Page(url=page.url, status=resp.status if resp else 0, text=text)
        except Exception as exc:  # noqa: BLE001 - playwright timeout/network errors
            raise SourceError(f"Браузер не смог открыть {url}: {exc}") from exc
        finally:
            if page is not None:
                await page.close()

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()
            self._pw = self._browser = self._context = None


class AutoFetcher:
    """HTTP first; after the first blocked page, use the browser for the rest of the run."""

    def __init__(self, *, use_proxy: bool = False, min_interval: float = 1.5) -> None:
        self.http = HttpFetcher(use_proxy=use_proxy, min_interval=min_interval)
        self.browser = BrowserFetcher(use_proxy=use_proxy, min_interval=min_interval)
        self._use_browser = False

    async def get(self, url: str, params: dict[str, Any] | None = None) -> Page:
        if not self._use_browser:
            page = await self.http.get(url, params)
            if not page.looks_blocked or not BrowserFetcher.available():
                return page
            log.info("%s looks blocked for plain HTTP, switching to browser", urlsplit(url).netloc)
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
    """GET a JSON API with the shared rate limiter; errors become SourceError."""
    key = ("json",) + _cache_key(url, params)
    if cache_ttl and (cached := response_cache.get(key)) is not None:
        return cached
    host = urlsplit(url).netloc
    await rate_limiter.wait(host, min_interval)
    proxy = get_settings().proxy_url if use_proxy else None
    async with httpx.AsyncClient(proxy=proxy, timeout=30.0, follow_redirects=True,
                                 headers={**BROWSER_HEADERS, "Accept": "application/json",
                                          **(headers or {})}) as client:
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise SourceError(f"{source or host} недоступен: {exc}") from exc
    if resp.status_code >= 400:
        raise SourceError(f"{source or host} ответил {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise SourceError(f"{source or host} вернул не JSON") from exc
    if cache_ttl:
        response_cache.put(key, data, cache_ttl)
    return data


def make_fetcher(mode: FetchMode = "auto", *, use_proxy: bool = False,
                 min_interval: float = 1.5, cache_ttl: float = 0.0) -> PageFetcher:
    if mode == "http":
        return HttpFetcher(use_proxy=use_proxy, min_interval=min_interval, cache_ttl=cache_ttl)
    if mode == "browser":
        return BrowserFetcher(use_proxy=use_proxy, min_interval=min_interval)
    return AutoFetcher(use_proxy=use_proxy, min_interval=min_interval)
