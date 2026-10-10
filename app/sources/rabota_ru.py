"""Работа.ру (rabota.ru) — general Russian job board, parsed from HTML pages.

Search:  GET https://www.rabota.ru/vacancy/?query=<q>&page=N (Nuxt SSR, ~1 MB). The page embeds
         one ``application/ld+json`` script with a list of JobPosting objects (~30 per page)
         that carry the FULL description, so search results are complete (not partial).
         The region is the one rabota.ru picks for the visitor (Moscow by default).
Card:    https://www.rabota.ru/vacancy/<id>/ has no JSON-LD: title/company/salary come from
         schema.org microdata, the description from the ``window.__NUXT__`` state script.

Markup verified live on 2026-10-08.

Options:
  fetcher       "http" | "auto" | "browser" (default "http"; "auto" escapes a captcha with a
                headless browser and must be chosen explicitly)
  pages         max result pages per query (1..5, default 2)
  min_interval  seconds between requests (default 2.0)
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft, as_dict, as_str, safe_map
from app.sources.jsonld import draft_from_job_posting, parse_date, walk
from app.sources.web import PageFetcher, make_fetcher

SITE = "https://www.rabota.ru"
_ID_RE = re.compile(r"rabota\.ru/vacancy/(\d+)")
PAGE_SIZE = 30


def _num(value: Any) -> int | None:
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n > 0 else None


def _salary(jp: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    """rabota.ru puts min/max on MonetaryAmount itself (baseSalary) or in its value (estimatedSalary)."""
    for key in ("baseSalary", "estimatedSalary"):
        money = as_dict(jp.get(key))
        value = as_dict(money.get("value"))
        sal_from = _num(money.get("minValue")) or _num(value.get("minValue"))
        sal_to = _num(money.get("maxValue")) or _num(value.get("maxValue"))
        if sal_from or sal_to:
            currency = as_str(money.get("currency"))
            return sal_from, sal_to, "RUR" if currency in (None, "RUB", "RUR") else currency
    return None, None, None


def _city(address: str | None) -> str | None:
    """'Центральный федеральный округ,Москва' -> 'Москва'."""
    if not address:
        return None
    return address.split(",")[-1].strip() or None


def _location(jp: dict[str, Any]) -> str | None:
    loc = jp.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    addr = as_dict(as_dict(loc).get("address"))
    return as_str(addr.get("addressLocality")) or _city(as_str(addr.get("streetAddress")))


def _remote(title: str, jp: dict[str, Any]) -> bool | None:
    if jp.get("jobLocationType") == "TELECOMMUTE":
        return True
    low = title.lower()
    return True if ("удален" in low or "удалён" in low or "remote" in low) else None


def posting_to_draft(jp: dict[str, Any]) -> VacancyDraft | None:
    m = _ID_RE.search(str(jp.get("url") or ""))
    if not m:
        return None
    vid = m.group(1)
    draft = draft_from_job_posting(jp, source="rabota_ru", external_id=vid, url=f"{SITE}/vacancy/{vid}/")
    if draft is None:
        return None
    sal_from, sal_to, currency = _salary(jp)
    return draft.updated(salary_from=sal_from, salary_to=sal_to, currency=currency,
                         location=_location(jp), remote=_remote(draft.title, jp))


def parse_search_page(html: str) -> list[VacancyDraft]:
    soup = BeautifulSoup(html, "html.parser")
    postings: list[dict[str, Any]] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        postings += [d for d in walk(data) if d.get("@type") == "JobPosting"]
    drafts: dict[str, VacancyDraft] = {}
    for d in safe_map(posting_to_draft, postings, source="Работа.ру"):
        drafts.setdefault(d.external_id, d)
    return list(drafts.values())


def _prop(root: Tag, name: str) -> str | None:
    el = root.find(attrs={"itemprop": name})
    if not isinstance(el, Tag):
        return None
    value = el.get("content") if el.name == "meta" else el.get_text(" ", strip=True)
    return (str(value).strip() or None) if value else None


def _nuxt_description(html: str, external_id: str) -> str:
    m = re.search(r"vacancy:\{vacancy:\{id:" + re.escape(external_id)
                  + r",.{0,5000}?\bdescription:\"((?:[^\"\\]|\\.)*)\"", html, re.S)
    if not m:
        return ""
    try:
        return html_to_text(json.loads('"' + m.group(1) + '"'))
    except json.JSONDecodeError:
        return ""


def parse_vacancy_page(html: str, external_id: str) -> VacancyDraft | None:
    soup = BeautifulSoup(html, "html.parser")
    title = _prop(soup, "title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else None
    if not title:
        return None
    org = soup.find(attrs={"itemprop": "hiringOrganization"})
    company = _prop(org, "name") if isinstance(org, Tag) else None
    salary = soup.find(attrs={"itemprop": "baseSalary"})
    sal_from = sal_to = None
    currency = None
    if isinstance(salary, Tag):
        sal_from, sal_to = _num(_prop(salary, "minValue")), _num(_prop(salary, "maxValue"))
        currency = _prop(salary, "currency")
    description = _nuxt_description(html, external_id)
    if not description:
        desc_el = soup.find(attrs={"itemprop": "description"})
        description = html_to_text(str(desc_el)) if isinstance(desc_el, Tag) else ""
    return VacancyDraft(
        source="rabota_ru",
        external_id=external_id,
        title=title[:300],
        url=f"{SITE}/vacancy/{external_id}/",
        company=company,
        location=_city(_prop(soup, "address")),
        salary_from=sal_from,
        salary_to=sal_to,
        currency=("RUR" if currency in (None, "RUB", "RUR") else currency) if (sal_from or sal_to) else None,
        remote=_remote(title, {}),
        employment=_prop(soup, "employmentType"),
        description=description,
        is_partial=not description,
        published_at=parse_date(_prop(soup, "datePosted")),
    )


class RabotaRuSource(JobSource):
    name = "rabota_ru"
    title = "Работа.ру"
    hint = "вакансии rabota.ru (регион по умолчанию — Москва)"
    checked_by_default = False

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
                params: dict[str, Any] = {"query": query.text}
                if page_no > 1:
                    params["page"] = page_no
                page = await fetcher.get(f"{SITE}/vacancy/", params)
                if page.looks_blocked:
                    raise SourceError("Работа.ру ограничила доступ (капча или блокировка)")
                if page.status >= 400:
                    raise SourceError(f"Работа.ру ответила {page.status}")
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
            page = await fetcher.get(f"{SITE}/vacancy/{external_id}/")
        finally:
            await fetcher.close()
        if page.status == 404:
            return None
        if page.status >= 400 or page.looks_blocked:
            raise SourceError(f"Работа.ру не отдала вакансию {external_id} (код {page.status})")
        found = safe_map(lambda h: parse_vacancy_page(h, external_id), [page.text], source=self.title)
        return found[0] if found else None

    def external_id_from_url(self, url: str) -> str | None:
        m = _ID_RE.search(url)
        return m.group(1) if m else None
