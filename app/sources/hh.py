"""hh.ru via its public REST API (https://api.hh.ru).

Options ([sources.hh.options] in config.toml):
  area          default region id (113 = Россия, 1 = Москва, 2 = Санкт-Петербург)
  user_agent    "AppName/1.0 (your@email)" — hh.ru asks clients to identify themselves
  access_token  optional OAuth token, sent as Bearer if hh.ru requires authorisation
  fetch_concurrency  parallel requests when loading full vacancy cards
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

import httpx

from app.core.http import make_async_client
from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft

API = "https://api.hh.ru"
_URL_RE = re.compile(r"hh\.(?:ru|kz|uz)/vacancy/(\d+)")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
    except ValueError:
        return None


def _is_remote(item: dict[str, Any]) -> bool | None:
    schedule = (item.get("schedule") or {}).get("id")
    formats = {f.get("id") for f in item.get("work_format") or [] if isinstance(f, dict)}
    if schedule == "remote" or "REMOTE" in formats:
        return True
    if schedule or formats:
        return False
    return None


def item_to_draft(item: dict[str, Any], *, full: bool) -> VacancyDraft:
    salary = item.get("salary") or item.get("salary_range") or {}
    snippet = item.get("snippet") or {}
    if full:
        description = html_to_text(item.get("description"))
    else:
        parts = [html_to_text(snippet.get("requirement")), html_to_text(snippet.get("responsibility"))]
        description = "\n".join(p for p in parts if p)
    return VacancyDraft(
        source="hh",
        external_id=str(item["id"]),
        title=item.get("name") or "",
        url=item.get("alternate_url"),
        company=(item.get("employer") or {}).get("name"),
        location=(item.get("area") or {}).get("name"),
        salary_from=salary.get("from"),
        salary_to=salary.get("to"),
        currency=salary.get("currency"),
        salary_gross=salary.get("gross"),
        remote=_is_remote(item),
        experience=(item.get("experience") or {}).get("name"),
        employment=(item.get("employment") or {}).get("name"),
        skills=[s.get("name", "") for s in item.get("key_skills") or [] if s.get("name")],
        description=description,
        is_partial=not full,
        published_at=_parse_dt(item.get("published_at")),
        raw=item if full else None,
    )


class HHSource(JobSource):
    name = "hh"
    title = "hh.ru"

    def _client(self) -> httpx.AsyncClient:
        opts = self.cfg.options
        ua = opts.get("user_agent") or "worksaas/0.1 (self-hosted)"
        headers = {"User-Agent": ua, "HH-User-Agent": ua}
        if opts.get("access_token"):
            headers["Authorization"] = f"Bearer {opts['access_token']}"
        return make_async_client(use_proxy=self.cfg.use_proxy, timeout=30.0, headers=headers)

    async def _get(self, client: httpx.AsyncClient, path: str, params: Any = None) -> dict[str, Any]:
        try:
            resp = await client.get(API + path, params=params)
        except httpx.HTTPError as exc:
            raise SourceError(f"hh.ru недоступен: {exc}") from exc
        if resp.status_code == 404:
            return {}
        if resp.status_code >= 400:
            raise SourceError(f"hh.ru ответил {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        f = query.filters
        params: list[tuple[str, Any]] = [
            ("text", query.text),
            ("per_page", min(limit, 100)),
            ("order_by", "publication_time"),
        ]
        area = f.area or str(self.cfg.options.get("area", ""))
        if area:
            params.append(("area", area))
        if f.period_days:
            params.append(("period", f.period_days))
        if f.salary_min:
            params.append(("salary", f.salary_min))
        if f.experience:
            params.append(("experience", f.experience))

        drafts: list[VacancyDraft] = []
        async with self._client() as client:
            page = 0
            while len(drafts) < limit:
                data = await self._get(client, "/vacancies", params + [("page", page)])
                items = data.get("items") or []
                drafts.extend(item_to_draft(i, full=False) for i in items)
                page += 1
                if not items or page >= int(data.get("pages") or 0):
                    break
        return drafts[:limit]

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        async with self._client() as client:
            data = await self._get(client, f"/vacancies/{external_id}")
        return item_to_draft(data, full=True) if data else None

    async def fetch_many(self, external_ids: list[str]) -> list[VacancyDraft]:
        sem = asyncio.Semaphore(int(self.cfg.options.get("fetch_concurrency", 3)))

        async def one(ext_id: str) -> VacancyDraft | None:
            async with sem:
                return await self.fetch(ext_id)

        results = await asyncio.gather(*(one(i) for i in external_ids), return_exceptions=True)
        return [r for r in results if isinstance(r, VacancyDraft)]

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
