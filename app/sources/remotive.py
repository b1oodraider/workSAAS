"""Remotive (remotive.com) — international remote jobs, public JSON API without a key.

API: https://remotive.com/api/remote-jobs?search=...&limit=N
Remotive asks API users to link back to the job URL and not to poll too often.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, VacancyDraft, safe_map
from app.sources.jsonld import parse_date
from app.sources.web import fetch_json

API = "https://remotive.com/api/remote-jobs"
_K_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[kK]")


def parse_usd_range(text: str | None) -> tuple[int | None, int | None]:
    """'$50k - $70k' -> (50000, 70000); '$4,000/month' -> (4000, None). Best effort."""
    if not text:
        return None, None
    ks = _K_RE.findall(text)
    if ks:
        nums = [int(float(k.replace(",", ".")) * 1000) for k in ks]
    else:
        nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]{2,}", text)]
    if not nums:
        return None, None
    return nums[0], (nums[1] if len(nums) > 1 else None)


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    if not item.get("id") or not item.get("title"):
        return None
    sal_from, sal_to = parse_usd_range(item.get("salary"))
    location = item.get("candidate_required_location")
    desc = html_to_text(item.get("description") or "")
    if location:
        desc = f"Требования к локации кандидата: {location}\n\n{desc}"
    return VacancyDraft(
        source="remotive",
        external_id=str(item["id"]),
        title=str(item["title"])[:300],
        url=item.get("url"),
        company=item.get("company_name"),
        location=location,
        salary_from=sal_from,
        salary_to=sal_to,
        currency="USD" if (sal_from or sal_to) and "$" in str(item.get("salary")) else None,
        remote=True,
        employment=item.get("job_type"),
        skills=[str(t) for t in item.get("tags") or []][:20],
        description=desc,
        published_at=parse_date(item.get("publication_date")),
    )


class RemotiveSource(JobSource):
    name = "remotive"
    title = "Remotive"
    hint = "удалёнка за рубежом, англ."
    checked_by_default = False

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        data = await fetch_json(API, params={"search": query.text, "limit": limit},
                                use_proxy=self.cfg.use_proxy, source=self.title, min_interval=2.0,
                                cache_ttl=3600)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        return safe_map(item_to_draft, jobs or [], source=self.title)[:limit]
