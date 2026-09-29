"""hh.ru: public REST API (https://api.hh.ru) with a fallback to parsing the website.

Options ([sources.hh.options] in config.toml):
  mode          "auto" (API, then website if the API refuses) | "api" | "web"
  fetcher       for web mode: "auto" (HTTP, browser if blocked) | "http" | "browser"
  min_interval  seconds between requests to hh.ru in web mode (default 2.5) — be polite
  area          default region id (113 = Россия, 1 = Москва, 2 = Санкт-Петербург)
  user_agent    "AppName/1.0 (your@email)" — hh.ru asks API clients to identify themselves
  access_token  optional OAuth token, sent as Bearer if the API requires authorisation
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

import httpx

from app.core.http import make_async_client
from app.core.text import html_to_text
from app.sources import hh_web
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft
from app.sources.web import make_fetcher

log = logging.getLogger(__name__)

API = "https://api.hh.ru"
SITE = "https://hh.ru"
# Web search accepts only these periods (days).
_WEB_PERIODS = (1, 3, 7, 30)
# When the API refuses anonymous access, don't hammer it again for a while (auto mode).
_API_RETRY_AFTER_S = 6 * 3600
_api_denied_until = 0.0
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


class ApiDenied(SourceError):
    """API refused access (401/403): auto mode falls back to the website."""


class HHSource(JobSource):
    name = "hh"
    title = "hh.ru"

    # --- mode selection -----------------------------------------------------

    @property
    def mode(self) -> str:
        return str(self.cfg.options.get("mode", "auto"))

    def _use_api(self) -> bool:
        if self.mode == "web":
            return False
        return self.mode == "api" or time.monotonic() >= _api_denied_until

    @staticmethod
    def _mark_api_denied() -> None:
        global _api_denied_until
        _api_denied_until = time.monotonic() + _API_RETRY_AFTER_S
        log.warning("hh.ru API refused access; using the website for %d h", _API_RETRY_AFTER_S // 3600)

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        if self._use_api():
            try:
                return await self._api_search(query, limit)
            except ApiDenied:
                if self.mode == "api":
                    raise
                self._mark_api_denied()
        return await self._web_search(query, limit)

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        if self._use_api():
            try:
                return await self._api_fetch(external_id)
            except ApiDenied:
                if self.mode == "api":
                    raise
                self._mark_api_denied()
        return await self._web_fetch(external_id)

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None

    # --- API ------------------------------------------------------------------

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
        if resp.status_code in (401, 403):
            raise ApiDenied(f"API hh.ru отказал в доступе ({resp.status_code})")
        if resp.status_code >= 400:
            raise SourceError(f"hh.ru ответил {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _area(self, query: SearchQuery) -> str:
        return query.filters.area or str(self.cfg.options.get("area", ""))

    async def _api_search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        f = query.filters
        params: list[tuple[str, Any]] = [
            ("text", query.text),
            ("per_page", min(limit, 100)),
            ("order_by", "publication_time"),
        ]
        if self._area(query):
            params.append(("area", self._area(query)))
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

    async def _api_fetch(self, external_id: str) -> VacancyDraft | None:
        async with self._client() as client:
            data = await self._get(client, f"/vacancies/{external_id}")
        return item_to_draft(data, full=True) if data else None

    # --- website --------------------------------------------------------------

    def _fetcher(self):
        return make_fetcher(
            self.cfg.options.get("fetcher", "auto"),
            use_proxy=self.cfg.use_proxy,
            min_interval=float(self.cfg.options.get("min_interval", 2.5)),
        )

    def _web_params(self, query: SearchQuery, page: int, per_page: int) -> dict[str, Any]:
        f = query.filters
        params: dict[str, Any] = {
            "text": query.text,
            "order_by": "publication_time",
            "items_on_page": per_page,
            "page": page,
        }
        if self._area(query):
            params["area"] = self._area(query)
        if f.period_days:
            params["search_period"] = next((p for p in _WEB_PERIODS if p >= f.period_days), 30)
        if f.salary_min:
            params["salary"] = f.salary_min
        if f.experience:
            params["experience"] = f.experience
        return params

    async def _web_search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        per_page = 50 if limit > 20 else 20
        drafts: dict[str, VacancyDraft] = {}
        fetcher = self._fetcher()
        try:
            page = 0
            while len(drafts) < limit and page < 10:
                resp = await fetcher.get(f"{SITE}/search/vacancy", self._web_params(query, page, per_page))
                if resp.looks_blocked:
                    raise SourceError(
                        "hh.ru показал капчу/блокировку. Включите браузерный режим "
                        "(fetcher = \"browser\") или прокси, либо увеличьте min_interval."
                    )
                if resp.status >= 400:
                    raise SourceError(f"hh.ru ответил {resp.status}")
                found = hh_web.parse_search_page(resp.text)
                new = [d for d in found if d.external_id not in drafts]
                for d in new:
                    drafts[d.external_id] = d
                page += 1
                if not new or not hh_web.has_next_page(resp.text):
                    break
        finally:
            await fetcher.close()
        return list(drafts.values())[:limit]

    async def _web_fetch(self, external_id: str) -> VacancyDraft | None:
        fetcher = self._fetcher()
        try:
            resp = await fetcher.get(f"{SITE}/vacancy/{external_id}")
        finally:
            await fetcher.close()
        if resp.status == 404:
            return None
        if resp.looks_blocked or resp.status >= 400:
            raise SourceError(f"hh.ru не отдал вакансию {external_id} (код {resp.status})")
        return hh_web.parse_vacancy_page(resp.text, external_id)
