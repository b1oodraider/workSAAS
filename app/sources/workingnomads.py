"""Working Nomads (workingnomads.com) — curated international remote jobs, public JSON feed.

API: https://www.workingnomads.com/api/exposed_jobs/ (a plain list of the latest jobs, no key).
The feed has no text search, so it is fetched at most once an hour (cached) and filtered
locally by query words. Links point back to the job page on Working Nomads.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.core.text import html_to_text, stable_hash
from app.sources.base import JobSource, SearchQuery, VacancyDraft, matches_query
from app.sources.jsonld import parse_date
from app.sources.web import fetch_json

API = "https://www.workingnomads.com/api/exposed_jobs/"
_URL_RE = re.compile(r"workingnomads\.com/jobs?/go/(\d+)")


def _tags(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    return [str(t).strip() for t in value if str(t).strip()] if isinstance(value, list) else []


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    url = item.get("url")
    if not url or not item.get("title"):
        return None
    m = _URL_RE.search(str(url))
    location = (item.get("location") or "").strip() or None
    desc = html_to_text(item.get("description") or "")
    if location:
        desc = f"Требования к локации кандидата: {location}\n\n{desc}"
    return VacancyDraft(
        source="workingnomads",
        external_id=m.group(1) if m else stable_hash(url)[:24],
        title=str(item["title"])[:300],
        url=str(url),
        company=item.get("company_name"),
        location=location,
        remote=True,
        skills=_tags(item.get("tags"))[:20],
        description=desc,
        published_at=parse_date(item.get("pub_date")),
    )


def _relevant(item: Any, query: str) -> bool:
    if not isinstance(item, dict):
        return False
    text = " ".join([str(item.get("title", "")), str(item.get("category_name", "")),
                     str(item.get("tags", "")), str(item.get("description", ""))[:2000]])
    return matches_query(text, query)


class WorkingNomadsSource(JobSource):
    name = "workingnomads"
    title = "Working Nomads"
    hint = "удалёнка за рубежом, англ."
    checked_by_default = False

    async def _feed(self) -> list[dict[str, Any]]:
        async def load() -> list[dict[str, Any]]:
            data = await fetch_json(API, use_proxy=self.cfg.use_proxy, source=self.title, min_interval=2.0,
                                    cache_ttl=3600)
            return [i for i in data if isinstance(i, dict)] if isinstance(data, list) else []
        return await self.memoized("feed", load)

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        drafts = [d for i in await self._feed() if _relevant(i, query.text)
                  and (d := self.parse_once(str(i.get("url")), item_to_draft, i)) is not None]
        drafts.sort(key=lambda d: d.published_at or datetime.min, reverse=True)
        return drafts[:limit]

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
