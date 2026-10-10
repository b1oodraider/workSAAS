"""Himalayas (himalayas.app) — international remote jobs, public JSON API without a key.

API: https://himalayas.app/jobs/api/search?q=...&limit=N (the server caps ``limit`` at 20).
Himalayas asks API users to link back to the job page and not to hammer the API;
responses are cached for an hour.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, VacancyDraft, safe_map
from app.sources.salary import known_currency
from app.sources.web import fetch_json

API = "https://himalayas.app/jobs/api/search"
PAGE_MAX = 20
_URL_RE = re.compile(r"himalayas\.app/companies/[^/?#]+/jobs/([^/?#]+)")
_PERIOD = {"annual": "в год", "yearly": "в год", "monthly": "в месяц", "hourly": "в час"}


def _from_unix(ts: Any) -> datetime | None:
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
    return None


def _strs(value: Any) -> list[str]:
    return [str(v) for v in value if v] if isinstance(value, list) else []


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    url = item.get("guid") or item.get("applicationLink")
    if not url or not item.get("title"):
        return None
    external_id = str(url).rstrip("/").rsplit("/", 1)[-1]
    sal_from = int(float(item["minSalary"])) if item.get("minSalary") else None
    sal_to = int(float(item["maxSalary"])) if item.get("maxSalary") else None
    currency = known_currency(item.get("currency"))
    countries = _strs(item.get("locationRestrictions"))
    location = ", ".join(countries)[:300] or None
    desc = html_to_text(item.get("description") or "")
    head = []
    if location:
        head.append(f"Требования к локации кандидата: {location}")
    if (sal_from or sal_to) and not currency:
        # Numbers without a known currency would mislead the ranking: keep them as text only.
        head.append(f"Зарплата: {sal_from or ''}–{sal_to or ''} {item.get('currency') or ''}".rstrip())
        sal_from = sal_to = None
    if (sal_from or sal_to) and item.get("salaryPeriod") in _PERIOD:
        head.append(f"Зарплата указана {_PERIOD[item['salaryPeriod']]}")
    if head:
        desc = "\n".join(head) + "\n\n" + desc
    return VacancyDraft(
        source="himalayas",
        external_id=external_id,
        title=str(item["title"])[:300],
        url=str(url),
        company=item.get("companyName"),
        location=location,
        salary_from=sal_from,
        salary_to=sal_to,
        currency=currency if (sal_from or sal_to) else None,
        remote=True,
        experience=", ".join(_strs(item.get("seniority"))) or None,
        employment=item.get("employmentType") or None,
        skills=[c.replace("-", " ") for c in _strs(item.get("categories"))][:20],
        description=desc,
        published_at=_from_unix(item.get("pubDate")),
    )


class HimalayasSource(JobSource):
    name = "himalayas"
    title = "Himalayas"
    hint = "удалёнка за рубежом, англ."
    checked_by_default = False

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        data = await fetch_json(API, params={"q": query.text, "limit": min(limit, PAGE_MAX)},
                                use_proxy=self.cfg.use_proxy, source=self.title, min_interval=2.0,
                                cache_ttl=3600)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        return safe_map(item_to_draft, jobs or [], source=self.title)[:limit]

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
