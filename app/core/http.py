"""Shared outbound HTTP client factory (httpx) with optional proxy."""

from __future__ import annotations

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
