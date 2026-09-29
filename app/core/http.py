"""Shared outbound HTTP client factory (httpx) with optional proxy."""

from __future__ import annotations

import httpx

from app.core.config import get_settings

USER_AGENT = "worksaas/0.1 (self-hosted job search assistant)"


def make_async_client(
    *, use_proxy: bool = False, timeout: float = 30.0, headers: dict[str, str] | None = None
) -> httpx.AsyncClient:
    settings = get_settings()
    proxy = settings.proxy_url if use_proxy else None
    return httpx.AsyncClient(
        proxy=proxy,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
        follow_redirects=True,
    )
