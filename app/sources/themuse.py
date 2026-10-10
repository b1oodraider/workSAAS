"""The Muse (themuse.com) — US/international company jobs, public JSON API without a key.

API: https://www.themuse.com/api/public/jobs?page=N&category=...&location=...
(20 jobs per page, pages start at 0; without a key The Muse allows 500 requests/hour).
The API has no text search: for each configured category up to ``options.pages`` pages
are fetched (cached for an hour) and filtered locally by query words.

Options:
  categories  The Muse category names (default ["Software Engineering", "Data and Analytics"])
  location    optional location filter, e.g. "Flexible / Remote"
  pages       pages per category (1..5, default 2)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft, as_dict, as_str, matches_query
from app.sources.jsonld import parse_date
from app.sources.web import fetch_json

log = logging.getLogger(__name__)

API = "https://www.themuse.com/api/public/jobs"
DEFAULT_CATEGORIES = ["Software Engineering", "Data and Analytics"]


def _names(value: Any) -> list[str]:
    return [n for n in (as_str(v) for v in value) if n] if isinstance(value, list) else []


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    if not item.get("id") or not item.get("name"):
        return None
    locations = _names(item.get("locations"))
    remote = any("remote" in loc.lower() for loc in locations)
    return VacancyDraft(
        source="themuse",
        external_id=str(item["id"]),
        title=str(item["name"]).strip()[:300],
        url=as_dict(item.get("refs")).get("landing_page"),
        company=as_str(item.get("company")),
        location="; ".join(locations)[:300] or None,
        remote=True if remote else None,
        experience=", ".join(_names(item.get("levels"))) or None,
        skills=_names(item.get("categories"))[:20],
        description=html_to_text(item.get("contents") or ""),
        published_at=parse_date(item.get("publication_date")),
    )


def _relevant(item: Any, query: str) -> bool:
    if not isinstance(item, dict):
        return False
    text = " ".join([str(item.get("name", "")), str(item.get("contents", ""))[:3000]])
    return matches_query(text, query)


class TheMuseSource(JobSource):
    name = "themuse"
    title = "The Muse"
    hint = "вакансии компаний США и мира, англ."
    checked_by_default = False

    def _options(self) -> tuple[list[str], str, int]:
        categories = self.opt_list("categories") or DEFAULT_CATEGORIES
        return categories, str(self.cfg.options.get("location") or ""), self.opt_int("pages", 2, lo=1, hi=5)

    async def _page(self, category: str, page: int, location: str) -> tuple[list[dict[str, Any]], bool]:
        """Items of one API page + whether it is the last one (memoised for the run)."""
        async def load() -> tuple[list[dict[str, Any]], bool]:
            params: dict[str, Any] = {"page": page, "category": category}
            if location:
                params["location"] = location
            data = await fetch_json(API, params=params, use_proxy=self.cfg.use_proxy,
                                    source=self.title, min_interval=1.0, cache_ttl=3600)
            results = data.get("results") if isinstance(data, dict) else None
            items = [i for i in results or [] if isinstance(i, dict)]
            page_count = data.get("page_count") if isinstance(data, dict) else None
            return items, not items or (isinstance(page_count, int) and page + 1 >= page_count)
        return await self.memoized(("page", category, page, location), load)

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        categories, location, pages = self._options()
        drafts: list[VacancyDraft] = []
        seen: set[str] = set()
        error: SourceError | None = None
        for category in categories:
            for page in range(pages):
                try:
                    items, last = await self._page(category, page, location)
                except SourceError as exc:
                    log.warning("%s: category %r page %s failed: %s", self.title, category, page, exc)
                    error = exc
                    break
                for item in items:
                    if not _relevant(item, query.text):
                        continue
                    d = self.parse_once(str(item.get("id")), item_to_draft, item)
                    if d is not None and d.external_id not in seen:
                        seen.add(d.external_id)
                        drafts.append(d)
                if last:
                    break
        if not drafts and error is not None:
            raise error
        drafts.sort(key=lambda d: d.published_at or datetime.min, reverse=True)
        return drafts[:limit]
