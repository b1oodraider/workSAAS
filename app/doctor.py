"""`worksaas doctor`: checks configuration and connectivity on the user's machine.

Everything that could not be verified during development (live job sites, LLM
resellers, Telegram) is checked here with one command.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import httpx
from pydantic import BaseModel

from app.core.config import get_settings

# Endpoints the enabled sources talk to (cheap GETs; no searches are run).
SOURCE_PROBES = {
    "hh": ["https://api.hh.ru/vacancies?text=python&per_page=1", "https://hh.ru/search/vacancy?text=python"],
    "habr": ["https://career.habr.com/api/frontend/vacancies?q=python&page=1"],
    "trudvsem": ["http://opendata.trudvsem.ru/api/v1/vacancies?text=python&limit=1"],
    "superjob": ["https://api.superjob.ru/2.0/vacancies/?keyword=python&count=1"],
    "telegram": ["https://t.me/s/telegram"],
    "remotive": ["https://remotive.com/api/remote-jobs?limit=1"],
    "remoteok": ["https://remoteok.com/api"],
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


class _Ping(BaseModel):
    ok: bool
    word: str


async def _probe(url: str, *, use_proxy: bool, headers: dict[str, str] | None = None) -> Check:
    from app.sources.web import BROWSER_HEADERS

    proxy = get_settings().proxy_url if use_proxy else None
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=15, follow_redirects=True,
                                     headers={**BROWSER_HEADERS, **(headers or {})}) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        return Check(url, False, f"нет соединения: {type(exc).__name__}")
    blocked = "captcha" in str(resp.url).lower() or resp.status_code in (401, 403, 429)
    return Check(url, resp.status_code < 400 and not blocked, f"HTTP {resp.status_code}"
                 + (" (капча/блок — попробуйте браузерный режим или прокси)" if blocked else ""))


async def check_sources() -> list[Check]:
    from app.sources.registry import SOURCE_CLASSES, source_config

    tasks = []
    for name in SOURCE_CLASSES:
        cfg = source_config(name)
        if not cfg.enabled or name not in SOURCE_PROBES:
            continue
        if name == "telegram" and not cfg.options.get("channels"):
            continue
        headers = {"X-Api-App-Id": os.environ.get("SUPERJOB_API_KEY", "")} if name == "superjob" else None
        for url in SOURCE_PROBES[name]:
            tasks.append((name, _probe(url, use_proxy=cfg.use_proxy, headers=headers)))
    results = await asyncio.gather(*(t for _, t in tasks))
    return [Check(f"источник {name}: {r.name}", r.ok, r.detail) for (name, _), r in zip(tasks, results)]


def check_config() -> list[Check]:
    s = get_settings()
    has_secret = s.secret_key not in ("", "change-me")
    checks = [Check("WS_SECRET_KEY задан", has_secret,
                    "" if has_secret else "без него сессии сбрасываются при перезапуске")]
    used = {t.provider for r in s.llm.routes.values() for t in r.targets()}
    for name in sorted(used):
        cfg = s.llm.providers.get(name)
        if cfg is None:
            checks.append(Check(f"LLM-провайдер {name}", False, "используется в маршрутах, но не описан"))
        elif cfg.type != "fake" and cfg.api_key_env:
            checks.append(Check(f"ключ провайдера {name} ({cfg.api_key_env})", bool(cfg.api_key),
                                "задайте в .env" if not cfg.api_key else ""))
    if s.sources.get("superjob") and s.sources["superjob"].enabled:
        checks.append(Check("SUPERJOB_API_KEY", bool(os.environ.get("SUPERJOB_API_KEY")), ""))
    from app.sources.web import BrowserFetcher

    checks.append(Check("браузерный режим (Playwright)", BrowserFetcher.available(),
                        "" if BrowserFetcher.available() else
                        "необязательно: pip install -e \".[browser]\" && playwright install chromium"))
    return checks


async def check_telegram() -> list[Check]:
    from app.bot.api import TelegramAPI, TelegramError

    cfg = get_settings().telegram
    if not cfg.active:
        return [Check("Telegram-бот", True, "не настроен (необязательно)")]
    api = TelegramAPI(cfg.bot_token or "", use_proxy=cfg.use_proxy)
    try:
        me = await api.get_me()
        return [Check("Telegram-бот", True, f"@{me.get('username')}")]
    except TelegramError as exc:
        return [Check("Telegram-бот", False, str(exc))]
    finally:
        await api.close()


async def check_llm() -> list[Check]:
    """One tiny real request per distinct provider/model used in routes (costs fractions of a cent)."""
    from app.core.config import LLMRoute
    from app.llm.base import LLMError, LLMRequest
    from app.llm.gateway import LLMGateway

    s = get_settings()
    gw = LLMGateway(s)
    seen, checks = set(), []
    for route in s.llm.routes.values():
        for t in route.targets():
            if (t.provider, t.model) in seen:
                continue
            seen.add((t.provider, t.model))
            req = LLMRequest(task="doctor", model=t.model, system="Ответь JSON по схеме.",
                             user='Верни ok=true и word="привет".', output_type=_Ping, max_tokens=2000,
                             effort=LLMRoute(provider=t.provider, model=t.model, effort=t.effort).effort)
            try:
                resp = await gw.provider(t.provider).generate(req)
                checks.append(Check(f"LLM {t.provider} / {t.model}", bool(resp.data.get("ok")),
                                    f"ответ: {resp.data}, токены {resp.usage.input_tokens}/{resp.usage.output_tokens}"))
            except LLMError as exc:
                checks.append(Check(f"LLM {t.provider} / {t.model}", False, str(exc)[:300]))
    return checks


async def run(*, llm: bool) -> list[Check]:
    checks = check_config()
    checks += await check_telegram()
    checks += await check_sources()
    if llm:
        checks += await check_llm()
    return checks
