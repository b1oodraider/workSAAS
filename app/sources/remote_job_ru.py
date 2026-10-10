"""Remote-job.ru — Russian board of remote (and some hybrid) vacancies, parsed from HTML.

Search:  GET https://remote-job.ru/search?search[query]=<q>&page=N — 20 ``div.vacancy_item``
         cards per page: title link /vacancy/show/<id>/<slug>, company, salary (h3) and a
         description snippet; no date. Results are partial.
Card:    https://remote-job.ru/vacancy/show/<id> (the slug is optional) — no JSON-LD/microdata:
         h1 title, company link (h2.h4), date "6 октября 2026", salary and experience in the
         panel heading, description in ``div.panel-body div.row.p-y-3 > div.col-md-12``.

Many cards are re-posts from other boards (the "Откликнуться" button may lead elsewhere).
Markup verified live on 2026-10-08.

Options:
  fetcher       "http" | "auto" | "browser" (default "http"; "auto" escapes a captcha with a
                headless browser and must be chosen explicitly)
  pages         max result pages per query (1..5, default 2)
  min_interval  seconds between requests (default 2.0)
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft, safe_map
from app.sources.salary import parse_salary_text
from app.sources.web import PageFetcher, make_fetcher

SITE = "https://remote-job.ru"
_URL_RE = re.compile(r"remote-job\.ru/vacancy/show/(\d+)")
_PATH_RE = re.compile(r"/vacancy/show/(\d+)")
_REMOTE_SUFFIX_RE = re.compile(r"\s*\(удал[её]нная работа\)\s*$", re.I)
_DATE_RE = re.compile(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", re.I)
_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
           "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
PAGE_SIZE = 20


def _text(el: Any) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if isinstance(el, Tag) else ""


def _title(raw: str) -> str:
    return _REMOTE_SUFFIX_RE.sub("", re.sub(r"\s+", " ", raw)).strip()


def parse_ru_date(text: str) -> datetime | None:
    """'6 октября 2026' -> datetime(2026, 10, 6)."""
    m = _DATE_RE.search(text or "")
    month = _MONTHS.get(m.group(2).lower()) if m else None
    if not m or not month:
        return None
    try:
        return datetime(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def _card_to_draft(card: Tag) -> VacancyDraft | None:
    link = card.select_one("h2 > a[href*='/vacancy/show/']")
    m = _PATH_RE.search(str(link.get("href", ""))) if isinstance(link, Tag) else None
    if not m:
        return None
    title = _title(link.get_text())  # no separator: search highlights split words into spans
    if not title:
        return None
    vid = m.group(1)
    salary = _text(card.select_one("h3"))
    sal_from, sal_to, currency, gross = parse_salary_text(salary)
    snippet = re.sub(r"^Описание вакансии\s*", "", _text(card.select_one("div.col-xs-12")))
    return VacancyDraft(
        source="remote_job_ru",
        external_id=vid,
        title=title[:300],
        url=f"{SITE}/vacancy/show/{vid}",
        company=_text(card.select_one("h2 small a")) or None,
        salary_from=sal_from,
        salary_to=sal_to,
        currency=currency,
        salary_gross=gross,
        remote=True,
        description=snippet,
        is_partial=True,
    )


def parse_search_page(html: str) -> list[VacancyDraft]:
    soup = BeautifulSoup(html, "html.parser")
    drafts: dict[str, VacancyDraft] = {}
    for d in safe_map(_card_to_draft, soup.select("div.vacancy_item"), source="Remote-job.ru"):
        drafts.setdefault(d.external_id, d)
    return list(drafts.values())


def _labelled(soup: BeautifulSoup, label: str) -> str:
    """Value under 'Уровень зарплаты:' / 'Требуемый опыт работы:' in the panel heading."""
    for col in soup.select("div.panel-heading div.col-md-4"):
        if label in col.get_text(" ", strip=True).lower():
            return _text(col.find("b"))
    return ""


def parse_vacancy_page(html: str, external_id: str) -> VacancyDraft | None:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    title = _title(h1.get_text()) if isinstance(h1, Tag) else ""
    if not title:
        return None
    body = soup.select_one("div.panel-body div.row.p-y-3 > div.col-md-12")
    description = ""
    if isinstance(body, Tag):
        for junk in body.select("h2, .alert, .depricated, form, script, style, button"):
            junk.decompose()
        description = html_to_text(str(body))
    date_el = next((el for el in soup.select("p.text-left small") if parse_ru_date(_text(el))), None)
    sal_from, sal_to, currency, gross = parse_salary_text(_labelled(soup, "зарплат"))
    experience = _labelled(soup, "опыт")
    return VacancyDraft(
        source="remote_job_ru",
        external_id=external_id,
        title=title[:300],
        url=f"{SITE}/vacancy/show/{external_id}",
        company=_text(soup.select_one("h2.h4 a")) or None,
        salary_from=sal_from,
        salary_to=sal_to,
        currency=currency,
        salary_gross=gross,
        remote=True,
        experience=experience if experience and experience.lower() != "не указан" else None,
        description=description,
        is_partial=not description,
        published_at=parse_ru_date(_text(date_el)) if date_el else None,
    )


class RemoteJobRuSource(JobSource):
    name = "remote_job_ru"
    title = "Remote-job.ru"
    hint = "удалённая работа в России"
    checked_by_default = False
    trusted = False  # mostly reposts from other boards: never the shared canonical copy

    def _fetcher(self) -> PageFetcher:
        # Plain HTTP by default: escaping a captcha with a headless browser is opt-in ("auto").
        # Pages are cached for an hour: the same query is not re-requested within a run.
        return make_fetcher(self.cfg.options.get("fetcher", "http"), use_proxy=self.cfg.use_proxy,
                            min_interval=self.opt_float("min_interval", 2.0, floor=2.0), cache_ttl=3600)

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        drafts: dict[str, VacancyDraft] = {}
        fetcher = self._fetcher()
        try:
            for page_no in range(1, self.opt_int("pages", 2, lo=1, hi=5) + 1):
                params: dict[str, Any] = {"search[query]": query.text}
                if page_no > 1:
                    params["page"] = page_no
                page = await fetcher.get(f"{SITE}/search", params)
                if page.looks_blocked:
                    raise SourceError("Remote-job.ru ограничил доступ (капча или блокировка)")
                if page.status >= 400:
                    raise SourceError(f"Remote-job.ru ответил {page.status}")
                found = parse_search_page(page.text)
                for d in found:
                    drafts.setdefault(d.external_id, d)
                if len(found) < PAGE_SIZE or len(drafts) >= limit:
                    break
        finally:
            await fetcher.close()
        return list(drafts.values())[:limit]

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        if not external_id.isdigit():
            return None
        fetcher = self._fetcher()
        try:
            page = await fetcher.get(f"{SITE}/vacancy/show/{external_id}")
        finally:
            await fetcher.close()
        if page.status == 404:
            return None
        if page.status >= 400 or page.looks_blocked:
            raise SourceError(f"Remote-job.ru не отдал вакансию {external_id} (код {page.status})")
        found = safe_map(lambda h: parse_vacancy_page(h, external_id), [page.text], source=self.title)
        return found[0] if found else None

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
