"""Arbeitnow (arbeitnow.com) — jobs in Germany/EU (many remote, many in German), public JSON feed.

API: https://www.arbeitnow.com/api/job-board-api?page=N (~325 jobs and ~2.4 MB per page,
newest first, updated hourly). The terms ask not to abuse the free API and to link back
to the job page. The feed has no text search: up to ``options.pages`` pages (default 2) are
fetched, cached for an hour and filtered locally by query words.

Options:
  pages  how many feed pages to scan (1..5, default 2)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, VacancyDraft, matches_query
from app.sources.web import fetch_json

API = "https://www.arbeitnow.com/api/job-board-api"
_URL_RE = re.compile(r"arbeitnow\.com/jobs/companies/[^/?#]+/([^/?#]+)")


def _from_unix(ts: Any) -> datetime | None:
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
    return None


def _strs(value: Any) -> list[str]:
    return [str(v) for v in value if v] if isinstance(value, list) else []


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    if not item.get("slug") or not item.get("title"):
        return None
    remote = item.get("remote")
    return VacancyDraft(
        source="arbeitnow",
        external_id=str(item["slug"]),
        title=str(item["title"])[:300],
        url=item.get("url"),
        company=item.get("company_name"),
        location=item.get("location") or None,
        remote=True if remote is True else None,
        employment=", ".join(_strs(item.get("job_types"))) or None,
        skills=_strs(item.get("tags"))[:20],
        description=html_to_text(item.get("description") or ""),
        published_at=_from_unix(item.get("created_at")),
    )


def _relevant(item: Any, query: str) -> bool:
    if not isinstance(item, dict):
        return False
    text = " ".join([str(item.get("title", "")), " ".join(_strs(item.get("tags"))),
                     str(item.get("description", ""))[:2000]])
    return matches_query(text, query)


class ArbeitnowSource(JobSource):
    name = "arbeitnow"
    title = "Arbeitnow"
    hint = "Германия и ЕС, англ./нем."
    checked_by_default = False

    async def _page(self, page: int) -> tuple[list[dict[str, Any]], bool]:
        """Relevant-candidate items of one feed page + whether a next page exists (memoised:
        the ~2.4 MB page is fetched and decoded once per run, not once per query)."""
        async def load() -> tuple[list[dict[str, Any]], bool]:
            data = await fetch_json(API, params={"page": page}, use_proxy=self.cfg.use_proxy,
                                    source=self.title, min_interval=2.0, cache_ttl=3600)
            items = data.get("data") if isinstance(data, dict) else None
            links = data.get("links") if isinstance(data, dict) else None
            items = [i for i in items or [] if isinstance(i, dict)]
            return items, bool(items) and isinstance(links, dict) and bool(links.get("next"))
        return await self.memoized(("page", page), load)

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        pages = self.opt_int("pages", 2, lo=1, hi=5)
        drafts: list[VacancyDraft] = []
        for page in range(1, pages + 1):
            items, has_next = await self._page(page)
            drafts += [d for i in items if _relevant(i, query.text)
                       and (d := self.parse_once(str(i.get("slug")), item_to_draft, i)) is not None]
            if len(drafts) >= limit or not has_next:
                break
        drafts.sort(key=lambda d: d.published_at or datetime.min, reverse=True)
        return drafts[:limit]

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
