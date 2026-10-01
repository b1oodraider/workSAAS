"""Foreign job boards with public JSON APIs (no key): remote jobs worldwide and Europe.

- Himalayas (himalayas.app/jobs/api/search?q=...) — remote jobs, server-side search.
- Jobicy (jobicy.com/api/v2/remote-jobs?tag=...) — remote jobs, keyword search.
- Arbeitnow (arbeitnow.com/api/job-board-api) — mostly Germany/EU; no server-side search,
  so a few pages are filtered here. The API terms ask to link back and not to poll hard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, VacancyDraft, matches_query, safe_map
from app.sources.jsonld import parse_date
from app.sources.web import fetch_json

REMOTE_HINT = "удалёнка за рубежом, англ."


def _epoch(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), UTC).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(float(value)) or None
    except (TypeError, ValueError, OverflowError):
        return None


def _list(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


# --------------------------------------------------------------------------- Himalayas

def himalayas_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    url = item.get("applicationLink") or item.get("guid")
    if not item.get("title") or not url:
        return None
    where = ", ".join(_list(item.get("locationRestrictions"))[:15]) or "любая страна"
    level = ", ".join(_list(item.get("seniority")))
    desc = html_to_text(item.get("description") or item.get("excerpt") or "")
    has_salary = item.get("minSalary") or item.get("maxSalary")
    return VacancyDraft(
        source="himalayas",
        external_id=str(url).rstrip("/").rsplit("/", 1)[-1][:120],
        title=str(item["title"])[:300],
        url=url,
        company=item.get("companyName"),
        location=f"удалённо: {where}",
        salary_from=_int(item.get("minSalary")),
        salary_to=_int(item.get("maxSalary")),
        currency=item.get("currency") if has_salary else None,
        remote=True,
        employment=item.get("employmentType"),
        experience=level or None,
        skills=_list(item.get("categories"))[:15],
        description=f"Уровень: {level or 'не указан'}. Страны: {where}.\n\n{desc}",
        published_at=_epoch(item.get("pubDate")),
    )


class HimalayasSource(JobSource):
    name = "himalayas"
    title = "Himalayas"
    hint = REMOTE_HINT
    checked_by_default = False

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        data = await fetch_json("https://himalayas.app/jobs/api/search",
                                params={"q": query.text, "limit": min(limit, 20)},
                                use_proxy=self.cfg.use_proxy, source=self.title, min_interval=2.0,
                                cache_ttl=3600)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        return safe_map(himalayas_to_draft, jobs or [], source=self.title)[:limit]


# --------------------------------------------------------------------------- Jobicy

_PERIODS = {"yearly": "в год", "monthly": "в месяц", "hourly": "в час"}


def jobicy_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    if not item.get("id") or not item.get("jobTitle"):
        return None
    geo = item.get("jobGeo") or "любая страна"
    level = item.get("jobLevel") or ""
    period = _PERIODS.get(str(item.get("salaryPeriod")), "")
    salary_note = f" Зарплата указана {period}." if period and item.get("salaryMin") else ""
    desc = html_to_text(item.get("jobDescription") or item.get("jobExcerpt") or "")
    return VacancyDraft(
        source="jobicy",
        external_id=str(item["id"]),
        title=str(item["jobTitle"])[:300],
        url=item.get("url"),
        company=item.get("companyName"),
        location=f"удалённо: {geo}",
        salary_from=_int(item.get("salaryMin")),
        salary_to=_int(item.get("salaryMax")),
        currency=item.get("salaryCurrency") if item.get("salaryMin") else None,
        remote=True,
        employment=", ".join(_list(item.get("jobType"))) or None,
        experience=level or None,
        skills=_list(item.get("jobIndustry")),
        description=f"Уровень: {level or 'не указан'}. Страны: {geo}.{salary_note}\n\n{desc}",
        published_at=parse_date(item.get("pubDate")),
    )


class JobicySource(JobSource):
    name = "jobicy"
    title = "Jobicy"
    hint = REMOTE_HINT
    checked_by_default = False

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        data = await fetch_json("https://jobicy.com/api/v2/remote-jobs",
                                params={"count": min(limit, 50), "tag": query.text},
                                use_proxy=self.cfg.use_proxy, source=self.title, min_interval=2.0,
                                cache_ttl=3600)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        return safe_map(jobicy_to_draft, jobs or [], source=self.title)[:limit]


# --------------------------------------------------------------------------- Arbeitnow

def arbeitnow_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    if not item.get("slug") or not item.get("title"):
        return None
    remote = bool(item.get("remote"))
    location = item.get("location") or ""
    return VacancyDraft(
        source="arbeitnow",
        external_id=str(item["slug"])[:120],
        title=str(item["title"])[:300],
        url=item.get("url") or f"https://www.arbeitnow.com/jobs/{item['slug']}",
        company=item.get("company_name"),
        location=(f"{location}, удалённо" if remote else location) or None,
        remote=remote or None,
        employment=", ".join(_list(item.get("job_types"))) or None,
        skills=_list(item.get("tags"))[:15],
        description=html_to_text(item.get("description") or ""),
        published_at=_epoch(item.get("created_at")),
    )


class ArbeitnowSource(JobSource):
    name = "arbeitnow"
    title = "Arbeitnow"
    hint = "Европа (в основном Германия), англ./нем."
    checked_by_default = False
    pages = 3

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        found: list[VacancyDraft] = []
        for page in range(1, int(self.cfg.options.get("pages", self.pages)) + 1):
            data = await fetch_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page},
                                    use_proxy=self.cfg.use_proxy, source=self.title, min_interval=2.0,
                                    cache_ttl=3600)
            items = data.get("data") if isinstance(data, dict) else None
            if not items:
                break
            for d in safe_map(arbeitnow_to_draft, items, source=self.title):
                if matches_query(f"{d.title} {' '.join(d.skills)}", query.text, all_words=True):
                    found.append(d)
            if len(found) >= limit or not (data.get("links") or {}).get("next"):
                break
        return found[:limit]
