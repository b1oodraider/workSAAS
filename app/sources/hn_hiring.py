"""Hacker News "Ask HN: Who is hiring?" — monthly thread of job posts, via the public Algolia API.

API (no key): https://hn.algolia.com/api/v1/
  search_by_date?tags=story,author_whoishiring&hitsPerPage=5  -> the latest monthly thread
  search?tags=comment,story_<id>&query=...&hitsPerPage=N      -> matching comments in it
Only top-level comments are job posts; their first line is usually
"Company | Role | Location | ...". Posts are written by arbitrary people, so the source is
untrusted. Responses are cached for an hour; links point to the comment on news.ycombinator.com.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, VacancyDraft, safe_map
from app.sources.jsonld import parse_date
from app.sources.web import fetch_json

API = "https://hn.algolia.com/api/v1"
THREAD_PREFIX = "ask hn: who is hiring"
ITEM_URL = "https://news.ycombinator.com/item?id={}"
HITS_MAX = 100
_URL_RE = re.compile(r"news\.ycombinator\.com/item\?id=(\d+)")


def latest_thread_id(data: Any) -> str | None:
    """objectID of the newest "Ask HN: Who is hiring?" story in a search_by_date response."""
    hits = data.get("hits") if isinstance(data, dict) else None
    threads = [h for h in hits or [] if isinstance(h, dict) and h.get("objectID")
               and str(h.get("title", "")).lower().startswith(THREAD_PREFIX)]
    if not threads:
        return None
    newest = max(threads, key=lambda h: h.get("created_at_i") or 0)
    return str(newest["objectID"])


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    if not item.get("objectID") or not item.get("comment_text"):
        return None
    text = html_to_text(item["comment_text"])
    first_line = next((line for line in text.splitlines() if line.strip()), "").strip()
    if not first_line:
        return None
    parts = [p.strip() for p in first_line.split("|")]
    company = parts[0] if len(parts) > 1 and 0 < len(parts[0]) <= 100 else None
    return VacancyDraft(
        source="hn_hiring",
        external_id=str(item["objectID"]),
        title=first_line[:200],
        url=ITEM_URL.format(item["objectID"]),
        company=company,
        remote=True if "remote" in first_line.lower() else None,
        description=text,
        published_at=parse_date(item.get("created_at")),
    )


class HNHiringSource(JobSource):
    name = "hn_hiring"
    title = "Hacker News: Who is hiring"
    hint = "ежемесячный тред HN, англ."
    checked_by_default = False
    trusted = False

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        stories = await fetch_json(f"{API}/search_by_date",
                                   params={"tags": "story,author_whoishiring", "hitsPerPage": 5},
                                   use_proxy=self.cfg.use_proxy, source=self.title, min_interval=1.0,
                                   cache_ttl=3600)
        thread = latest_thread_id(stories)
        if thread is None:
            return []
        # Replies to job posts match too and are dropped below, so ask for some extra hits.
        data = await fetch_json(f"{API}/search",
                                params={"tags": f"comment,story_{thread}", "query": query.text,
                                        "hitsPerPage": min(max(limit * 2, 20), HITS_MAX)},
                                use_proxy=self.cfg.use_proxy, source=self.title, min_interval=1.0,
                                cache_ttl=3600)
        hits = data.get("hits") if isinstance(data, dict) else None
        top_level = [h for h in hits or [] if isinstance(h, dict) and str(h.get("parent_id")) == thread]
        drafts = safe_map(item_to_draft, top_level, source=self.title)
        drafts.sort(key=lambda d: d.published_at or datetime.min, reverse=True)
        return drafts[:limit]

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
