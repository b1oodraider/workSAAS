"""RemoteOK (remoteok.com) — international remote jobs, public JSON feed.

API: https://remoteok.com/api (first element is a legal notice). RemoteOK's terms
require linking back to the job URL and mentioning RemoteOK as the source.
The feed has no text search, so results are filtered locally by query words.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, VacancyDraft, matches_query, safe_map
from app.sources.jsonld import parse_date
from app.sources.web import fetch_json

API = "https://remoteok.com/api"


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    if not item.get("id") or not item.get("position"):
        return None
    sal_from = item.get("salary_min") or None
    sal_to = item.get("salary_max") or None
    return VacancyDraft(
        source="remoteok",
        external_id=str(item["id"]),
        title=str(item["position"])[:300],
        url=item.get("url") or item.get("apply_url"),
        company=item.get("company"),
        location=item.get("location") or None,
        salary_from=sal_from,
        salary_to=sal_to,
        currency="USD" if (sal_from or sal_to) else None,
        remote=True,
        skills=[str(t) for t in item.get("tags") or []][:20],
        description=html_to_text(item.get("description") or ""),
        published_at=parse_date(item.get("date")),
    )


class RemoteOKSource(JobSource):
    name = "remoteok"
    title = "RemoteOK"
    hint = "удалёнка за рубежом, англ."
    checked_by_default = False

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        # The whole feed is fetched once per ~10 minutes and filtered locally for each query.
        data = await fetch_json(API, use_proxy=self.cfg.use_proxy, source=self.title, min_interval=5.0,
                                cache_ttl=600)
        items = [i for i in data or [] if isinstance(i, dict) and "position" in i]
        def relevant(item: dict) -> bool:
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            text = " ".join([str(item.get("position", "")), " ".join(map(str, tags)),
                             str(item.get("description", ""))[:2000]])
            return matches_query(text, query.text)

        drafts = safe_map(item_to_draft, [i for i in items if relevant(i)], source=self.title)
        drafts.sort(key=lambda d: d.published_at or datetime.min, reverse=True)
        return drafts[:limit]
