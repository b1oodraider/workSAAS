"""Jobicy (jobicy.com) — international remote jobs, public JSON API without a key.

API: https://jobicy.com/api/v2/remote-jobs?count=N&tag=... (count up to 50; docs: https://jobi.cy/apidocs).
Jobicy asks to credit it with a link to the original job URL and not to poll the API more
than a few times per hour, so responses are cached for an hour.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, VacancyDraft, safe_map
from app.sources.jsonld import parse_date
from app.sources.salary import known_currency
from app.sources.web import fetch_json

API = "https://jobicy.com/api/v2/remote-jobs"
PAGE_MAX = 50
_URL_RE = re.compile(r"jobicy\.com/jobs/(\d+)")
_PERIOD = {"yearly": "в год", "annual": "в год", "monthly": "в месяц", "hourly": "в час"}


def _strs(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in value if v] if isinstance(value, list) else []


def _num(value: Any) -> int | None:
    try:
        return int(float(value)) or None
    except (TypeError, ValueError):
        return None


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    if not item.get("id") or not item.get("jobTitle"):
        return None
    # v2 returns salaryMin/salaryMax; older responses used annualSalaryMin/annualSalaryMax.
    sal_from = _num(item.get("salaryMin") or item.get("annualSalaryMin"))
    sal_to = _num(item.get("salaryMax") or item.get("annualSalaryMax"))
    period = item.get("salaryPeriod") or ("yearly" if item.get("annualSalaryMin") else None)
    currency = known_currency(item.get("salaryCurrency"))
    location = (item.get("jobGeo") or "").strip() or None
    desc = html_to_text(item.get("jobDescription") or item.get("jobExcerpt") or "")
    head = []
    if location:
        head.append(f"Требования к локации кандидата: {location}")
    if (sal_from or sal_to) and not currency:
        # Numbers without a known currency would mislead the ranking: keep them as text only.
        head.append(f"Зарплата: {sal_from or ''}–{sal_to or ''} {item.get('salaryCurrency') or ''}".rstrip())
        sal_from = sal_to = None
    if (sal_from or sal_to) and period in _PERIOD:
        head.append(f"Зарплата указана {_PERIOD[period]}")
    if head:
        desc = "\n".join(head) + "\n\n" + desc
    level = item.get("jobLevel")
    return VacancyDraft(
        source="jobicy",
        external_id=str(item["id"]),
        title=html_to_text(str(item["jobTitle"]))[:300],
        url=item.get("url"),
        company=item.get("companyName"),
        location=location,
        salary_from=sal_from,
        salary_to=sal_to,
        currency=currency if (sal_from or sal_to) else None,
        remote=True,
        experience=level if level and level != "Any" else None,
        employment=", ".join(_strs(item.get("jobType"))) or None,
        skills=_strs(item.get("jobIndustry"))[:20],
        description=desc,
        published_at=parse_date(item.get("pubDate")),
    )


class JobicySource(JobSource):
    name = "jobicy"
    title = "Jobicy"
    hint = "удалёнка за рубежом, англ."
    checked_by_default = False

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        data = await fetch_json(API, params={"count": min(limit, PAGE_MAX), "tag": query.text},
                                use_proxy=self.cfg.use_proxy, source=self.title, min_interval=2.0,
                                cache_ttl=3600)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        return safe_map(item_to_draft, jobs or [], source=self.title)[:limit]

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
