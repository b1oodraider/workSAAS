"""schema.org JobPosting (JSON-LD) extraction.

Most job sites embed it for Google for Jobs, so it is the most stable way to read
a vacancy page: hh.ru, Habr Career and arbitrary URLs imported by users.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from app.core.text import html_to_text
from app.sources.base import VacancyDraft, as_str, safe_map


def walk(obj: Any) -> Iterator[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def find_job_posting(soup: BeautifulSoup) -> dict[str, Any] | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for d in walk(data):
            t = d.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                return d
    return None


def parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _num(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return None


def draft_from_job_posting(jp: dict[str, Any], *, source: str, external_id: str,
                           url: str | None) -> VacancyDraft | None:
    title = jp.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    company = as_str(jp.get("hiringOrganization"))

    location = None
    loc = jp.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        addr = loc.get("address")
        if isinstance(addr, dict):
            location = (as_str(addr.get("addressLocality")) or as_str(addr.get("addressRegion"))
                        or as_str(addr.get("addressCountry")))
        else:
            location = as_str(addr)

    sal_from = sal_to = None
    currency = None
    base = jp.get("baseSalary")
    if isinstance(base, dict):
        currency = as_str(base.get("currency"))
        value = base.get("value")
        if isinstance(value, dict):
            sal_from = _num(value.get("minValue"))
            sal_to = _num(value.get("maxValue"))
            if sal_from is None and sal_to is None:
                sal_from = _num(value.get("value"))
        else:
            sal_from = _num(value)
    if currency in ("RUB", "rub", "rur"):
        currency = "RUR"

    employment = jp.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(str(e) for e in employment)

    skills = jp.get("skills")
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",") if s.strip()]
    elif not isinstance(skills, list):
        skills = []

    description = html_to_text(jp.get("description") or "")
    return VacancyDraft(
        source=source,
        external_id=external_id,
        title=title.strip()[:300],
        url=url or as_str(jp.get("url")),
        company=company,
        location=location,
        salary_from=sal_from,
        salary_to=sal_to,
        currency=currency,
        remote=True if jp.get("jobLocationType") == "TELECOMMUTE" else None,
        employment=employment if isinstance(employment, str) else None,
        skills=[str(s) for s in skills][:30],
        description=description,
        is_partial=not description,
        published_at=parse_date(jp.get("datePosted")),
    )


def draft_from_html(html: str, *, source: str, external_id: str, url: str | None) -> VacancyDraft | None:
    jp = find_job_posting(BeautifulSoup(html, "html.parser"))
    if not jp:
        return None
    found = safe_map(lambda j: draft_from_job_posting(j, source=source, external_id=external_id, url=url),
                     [jp], source=source)
    return found[0] if found else None
