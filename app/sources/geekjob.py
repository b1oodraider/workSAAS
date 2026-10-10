"""Geekjob (geekjob.ru) — IT & digital vacancies (Russia, CIS, remote).

Search:  GET https://geekjob.ru/json/find/vacancy?page=N&qs=<q>[&rm=1] — the JSON endpoint the
         site's own search page calls (not a documented API). Items are short: position,
         company, city/country, salary string, jobFormat flags, id, sortOrder (YYYYMMDD).
         ``nextpage`` is 0 on the last page.
Card:    https://geekjob.ru/vacancy/<24-hex id> — JSON-LD JobPosting with the full description;
         the HTML header gives the location as shown on the page (the JSON-LD address may
         differ), work format, experience and level.

Markup verified live on 2026-10-08.

Options:
  fetcher       "http" | "auto" | "browser" for vacancy pages (default "http"; "auto" escapes a
                captcha with a headless browser and must be chosen explicitly)
  pages         max result pages per query (1..5, default 2)
  min_interval  seconds between requests (default 2.0)
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft, as_dict, as_str, safe_map
from app.sources.jsonld import draft_from_html
from app.sources.salary import parse_salary_text
from app.sources.web import PageFetcher, fetch_json, make_fetcher

SITE = "https://geekjob.ru"
API = f"{SITE}/json/find/vacancy"
_URL_RE = re.compile(r"geekjob\.ru/vacancy/([0-9a-f]{24})")
_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_FORMATS = {"remote": "удалённо", "relocate": "релокация", "parttime": "частичная занятость",
            "inhouse": "в офисе"}


def _date(sort_order: Any) -> datetime | None:
    try:
        return datetime.strptime(str(sort_order)[:8], "%Y%m%d")
    except ValueError:
        return None


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    vid = str(item.get("id") or "")
    title = as_str(item.get("position"))
    if not _ID_RE.match(vid) or not title:
        return None
    fmt = as_dict(item.get("jobFormat"))
    location = ", ".join(x for x in (as_str(item.get("city")), as_str(item.get("country"))) if x) or None
    salary = as_str(item.get("salary")) or ""
    sal_from, sal_to, currency, gross = parse_salary_text(salary)
    lines = []
    formats = [label for key, label in _FORMATS.items() if fmt.get(key)]
    if formats:
        lines.append("Формат: " + ", ".join(formats))
    if salary:
        lines.append(f"Зарплата: {salary}")
    return VacancyDraft(
        source="geekjob",
        external_id=vid,
        title=title[:300],
        url=f"{SITE}/vacancy/{vid}",
        company=as_str(as_dict(item.get("company")).get("name")),
        location=location,
        salary_from=sal_from,
        salary_to=sal_to,
        currency=currency,
        salary_gross=gross,
        remote=True if fmt.get("remote") else None,
        description="\n".join(lines),
        is_partial=True,
        published_at=_date(item.get("sortOrder")),
    )


def _text(el: Any) -> str:
    return el.get_text(" ", strip=True) if isinstance(el, Tag) else ""


def parse_vacancy_page(html: str, external_id: str) -> VacancyDraft | None:
    url = f"{SITE}/vacancy/{external_id}"
    soup = BeautifulSoup(html, "html.parser")
    header = soup.select_one("article.vacancy header") or soup
    draft = draft_from_html(html, source="geekjob", external_id=external_id, url=url)
    if draft is None:
        title = _text(header.find("h1"))
        if not title:
            return None
        company_el = header.select_one(".company-name a")
        draft = VacancyDraft(source="geekjob", external_id=external_id, url=url, title=title[:300],
                             company=_text(company_el) or None)
    update: dict[str, Any] = {}
    if not draft.description:
        body = soup.select_one("#vacancy-description")
        if isinstance(body, Tag):
            update["description"] = html_to_text(str(body))
            update["is_partial"] = not update["description"]
    location = _text(header.select_one(".location"))
    if location:
        update["location"] = location
    jobformat = soup.select_one(".jobinfo .jobformat")
    if isinstance(jobformat, Tag):
        lines = [s.strip() for s in jobformat.get_text("\n").splitlines() if s.strip()]
        if any("удален" in s.lower() or "удалён" in s.lower() for s in lines):
            update["remote"] = True
        exp = next((s for s in lines if s.lower().startswith("опыт")), None)
        if exp:
            update["experience"] = exp
    level = _text(header.select_one(".category"))
    if level and not update.get("experience"):
        update["experience"] = level
    salary = _text(soup.select_one(".jobinfo .salary"))
    if salary:
        sal_from, sal_to, currency, gross = parse_salary_text(salary)
        if sal_from or sal_to:
            update.update(salary_from=sal_from, salary_to=sal_to, currency=currency or draft.currency,
                          salary_gross=gross)
    tags = [t.strip() for t in _text(header.select_one(".tags")).split("•") if t.strip()]
    if tags and not draft.skills:
        update["skills"] = tags[:30]
    return draft.updated(**update)


class GeekjobSource(JobSource):
    name = "geekjob"
    title = "Geekjob"
    hint = "IT и диджитал, Россия и удалёнка"
    checked_by_default = True

    def _interval(self) -> float:
        return self.opt_float("min_interval", 2.0, floor=2.0)

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        drafts: dict[str, VacancyDraft] = {}
        for page_no in range(1, self.opt_int("pages", 2, lo=1, hi=5) + 1):
            params: dict[str, Any] = {"page": page_no, "qs": query.text}
            if query.filters.remote_only:
                params["rm"] = 1
            # Cached for an hour: the same query is not re-requested within a run.
            data = await fetch_json(API, params=params, use_proxy=self.cfg.use_proxy, source=self.title,
                                    min_interval=self._interval(), cache_ttl=3600)
            items = data.get("data") if isinstance(data, dict) else None
            if not items:
                break
            for d in safe_map(item_to_draft, items, source=self.title):
                drafts.setdefault(d.external_id, d)
            if len(drafts) >= limit or not data.get("nextpage"):
                break
        return list(drafts.values())[:limit]

    def _fetcher(self) -> PageFetcher:
        # Plain HTTP by default: escaping a captcha with a headless browser is opt-in ("auto").
        return make_fetcher(self.cfg.options.get("fetcher", "http"), use_proxy=self.cfg.use_proxy,
                            min_interval=self._interval(), cache_ttl=3600)

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        if not _ID_RE.match(external_id):
            return None
        fetcher = self._fetcher()
        try:
            page = await fetcher.get(f"{SITE}/vacancy/{external_id}")
        finally:
            await fetcher.close()
        if page.status == 404:
            return None
        if page.status >= 400 or page.looks_blocked:
            raise SourceError(f"Geekjob не отдал вакансию {external_id} (код {page.status})")
        found = safe_map(lambda h: parse_vacancy_page(h, external_id), [page.text], source=self.title)
        return found[0] if found else None

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
