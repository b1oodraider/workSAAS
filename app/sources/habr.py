"""Habr Career (career.habr.com) — IT vacancies.

Search uses the JSON endpoint of the site's own frontend (not a documented public
API, may change); vacancy pages are parsed via JSON-LD with an HTML fallback.

Options:
  fetcher       "auto" | "http" | "browser" for vacancy pages
  min_interval  seconds between requests (default 1.5)
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft
from app.sources.jsonld import draft_from_html, parse_date
from app.sources.web import fetch_json, make_fetcher

SITE = "https://career.habr.com"
_URL_RE = re.compile(r"career\.habr\.com/vacancies/(\d+)")


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    vid = item.get("id")
    title = item.get("title")
    if not vid or not title:
        return None
    salary = item.get("salary") or {}
    company = item.get("company") or {}
    skills = [s.get("title") for s in item.get("skills") or [] if isinstance(s, dict) and s.get("title")]
    locations = [loc.get("title") for loc in item.get("locations") or [] if isinstance(loc, dict)]
    qualification = (item.get("salaryQualification") or item.get("qualification") or {})
    divisions = [d.get("title") for d in item.get("divisions") or [] if isinstance(d, dict)]
    lines = []
    if qualification.get("title"):
        lines.append(f"Квалификация: {qualification['title']}")
    if divisions:
        lines.append("Направление: " + ", ".join(filter(None, divisions)))
    if skills:
        lines.append("Навыки: " + ", ".join(skills))
    currency = (salary.get("currency") or "").upper() or None
    published = item.get("publishedDate")
    return VacancyDraft(
        source="habr",
        external_id=str(vid),
        title=str(title)[:300],
        url=SITE + item["href"] if str(item.get("href", "")).startswith("/") else f"{SITE}/vacancies/{vid}",
        company=company.get("title") if isinstance(company, dict) else None,
        location=", ".join(filter(None, locations)) or None,
        salary_from=salary.get("from"),
        salary_to=salary.get("to"),
        currency="RUR" if currency in ("RUR", "RUB") else currency,
        remote=bool(item.get("remoteWork")) if item.get("remoteWork") is not None else None,
        experience=qualification.get("title"),
        skills=skills,
        description="\n".join(lines),
        is_partial=True,
        published_at=parse_date(published.get("date") if isinstance(published, dict) else published),
    )


def parse_vacancy_page(html: str, external_id: str) -> VacancyDraft | None:
    url = f"{SITE}/vacancies/{external_id}"
    draft = draft_from_html(html, source="habr", external_id=external_id, url=url)
    if draft and draft.description:
        return draft
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one(".vacancy-description__text") or soup.select_one(".style-ugc")
    title = soup.select_one("h1")
    if not body or not title:
        return draft
    base = draft or VacancyDraft(source="habr", external_id=external_id, url=url,
                                 title=title.get_text(" ", strip=True))
    return base.model_copy(update={"description": html_to_text(str(body)), "is_partial": False})


class HabrSource(JobSource):
    name = "habr"
    title = "Хабр Карьера"
    hint = "IT-вакансии"

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        drafts: list[VacancyDraft] = []
        page = 1
        while len(drafts) < limit and page <= 5:
            params: dict[str, Any] = {"q": query.text, "sort": "date", "type": "all", "page": page}
            if query.filters.remote_only:
                params["remote"] = "true"
            if query.filters.salary_min:
                params["salary"] = query.filters.salary_min
                params["currency"] = "RUR"
            data = await fetch_json(f"{SITE}/api/frontend/vacancies", params=params,
                                    use_proxy=self.cfg.use_proxy, source=self.title,
                                    min_interval=float(self.cfg.options.get("min_interval", 1.5)))
            items = data.get("list") if isinstance(data, dict) else None
            if not items:
                break
            drafts += [d for d in (item_to_draft(i) for i in items if isinstance(i, dict)) if d]
            meta = data.get("meta") or {}
            if page >= int(meta.get("totalPages") or 1):
                break
            page += 1
        return drafts[:limit]

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        fetcher = make_fetcher(self.cfg.options.get("fetcher", "auto"), use_proxy=self.cfg.use_proxy,
                               min_interval=float(self.cfg.options.get("min_interval", 1.5)))
        try:
            page = await fetcher.get(f"{SITE}/vacancies/{external_id}")
        finally:
            await fetcher.close()
        if page.status == 404:
            return None
        if page.status >= 400 or page.looks_blocked:
            raise SourceError(f"Хабр Карьера не отдала вакансию {external_id} (код {page.status})")
        return parse_vacancy_page(page.text, external_id)

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
