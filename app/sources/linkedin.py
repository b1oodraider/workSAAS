"""LinkedIn Jobs via the public guest endpoints (no login, no API key).

Search:  GET https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
             ?keywords=<q>&location=<loc>&start=0[&f_TPR=r<seconds>][&f_WT=2]&sortBy=DD
         -> HTML fragment of ~10 ``<li>`` cards (div.base-search-card[data-entity-urn=
            urn:li:jobPosting:<id>]): title, company, location, <time datetime>. Partial.
Card:    GET https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<id> -> HTML fragment with
         the full description (div.show-more-less-html__markup) and job criteria
         (seniority, employment type). Labels are localised by Accept-Language.

WARNING: LinkedIn's User Agreement forbids automated access (scraping). This source is opt-in
(``enabled_by_default = False``), slow (``min_interval`` >= 5 s) and reads at most ONE page
(~10 jobs) per query and location. It may break at any time, return 429, or get the IP
temporarily banned — use it sparingly and at your own risk.

Only job POSTINGS are available here. LinkedIn POSTS (feed updates "We're hiring…", which is how
many Russian-speaking recruiters publish vacancies) require a logged-in session and are not
reachable via the guest API; import such posts via manual paste / "import by link" instead.

Markup verified live on 2026-10-08.

Options:
  locations     list of LinkedIn location names (default ["Worldwide"]),
                e.g. ["Germany", "Netherlands", "European Union"]; at most 5 are used
  min_interval  seconds between requests (default 5.0, lower values are raised to 5)
  fetcher       "http" | "auto" | "browser" (default "http"; "auto" escapes a captcha with a
                headless browser and must be chosen explicitly)
  max_queries   only the first N queries of a search run are sent (default 3)
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft, safe_map
from app.sources.jsonld import parse_date
from app.sources.web import PageFetcher, make_fetcher

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
CARD_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
VIEW_URL = "https://www.linkedin.com/jobs/view/{id}/"
MAX_LOCATIONS = 5
_URN_RE = re.compile(r"jobPosting:(\d+)")
_URL_RES = (
    re.compile(r"linkedin\.com/jobs/view/(?:[^/?#]*?-)?(\d{6,})(?=[/?#]|$)"),
    re.compile(r"linkedin\.com/jobs/.*[?&]currentJobId=(\d{6,})"),
    re.compile(r"linkedin\.com/jobs-guest/jobs/api/jobPosting/(\d{6,})"),
)
_REMOTE_WORDS = ("remote", "удалённ", "удаленн", "удалённо", "удаленно", "home office", "homeoffice")
_SENIORITY = ("seniority level", "должностной уровень", "уровень")
_EMPLOYMENT = ("employment type", "тип занятости")


def _text(el: Any) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if isinstance(el, Tag) else ""


def _remote(*texts: str) -> bool | None:
    low = " ".join(texts).lower()
    return True if any(w in low for w in _REMOTE_WORDS) else None


def _card_to_draft(card: Tag) -> VacancyDraft | None:
    m = _URN_RE.search(str(card.get("data-entity-urn") or ""))
    if not m:
        return None
    title = _text(card.select_one(".base-search-card__title"))
    if not title:
        return None
    jid = m.group(1)
    location = _text(card.select_one(".job-search-card__location")) or None
    time_el = card.select_one("time[datetime]")
    salary = _text(card.select_one(".job-search-card__salary-info"))
    return VacancyDraft(
        source="linkedin",
        external_id=jid,
        title=title[:300],
        url=VIEW_URL.format(id=jid),
        company=_text(card.select_one(".base-search-card__subtitle")) or None,
        location=location,
        remote=_remote(title, location or ""),
        description=f"Зарплата: {salary}" if salary else "",
        is_partial=True,
        published_at=parse_date(str(time_el["datetime"])) if isinstance(time_el, Tag) else None,
    )


def parse_search_page(html: str) -> list[VacancyDraft]:
    soup = BeautifulSoup(html, "html.parser")
    drafts: dict[str, VacancyDraft] = {}
    for d in safe_map(_card_to_draft, soup.select("[data-entity-urn*='jobPosting:']"), source="LinkedIn"):
        drafts.setdefault(d.external_id, d)
    return list(drafts.values())


def _criteria(soup: BeautifulSoup) -> dict[str, str]:
    result: dict[str, str] = {}
    for li in soup.select("li.description__job-criteria-item"):
        label = _text(li.select_one("h3")).lower()
        value = _text(li.select_one("span"))
        if label and value:
            result[label] = value
    return result


def _pick(criteria: dict[str, str], labels: tuple[str, ...]) -> str | None:
    return next((v for k, v in criteria.items() if k in labels), None)


def parse_job_page(html: str, external_id: str) -> VacancyDraft | None:
    soup = BeautifulSoup(html, "html.parser")
    title = _text(soup.select_one(".top-card-layout__title, .topcard__title"))
    if not title:
        return None
    location = _text(soup.select_one(".topcard__flavor--bullet")) or None
    body = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
    description = html_to_text(str(body)) if isinstance(body, Tag) else ""
    criteria = _criteria(soup)
    salary = _text(soup.select_one(".salary, .compensation__salary"))
    if salary:
        description = f"Зарплата: {salary}\n\n{description}".strip()
    return VacancyDraft(
        source="linkedin",
        external_id=external_id,
        title=title[:300],
        url=VIEW_URL.format(id=external_id),
        company=_text(soup.select_one(".topcard__org-name-link, .topcard__flavor a")) or None,
        location=location,
        remote=_remote(title, location or ""),
        experience=_pick(criteria, _SENIORITY),
        employment=_pick(criteria, _EMPLOYMENT),
        description=description,
        is_partial=not body,
    )


class LinkedInSource(JobSource):
    name = "linkedin"
    title = "LinkedIn"
    hint = "вакансии на LinkedIn (без входа); посты не ищутся"
    enabled_by_default = False
    checked_by_default = False
    # Anyone can post a job on LinkedIn (no moderation comparable to a job board), so its
    # copy must never become the shared canonical vacancy.
    trusted = False

    def _locations(self) -> list[str]:
        return (self.opt_list("locations") or ["Worldwide"])[:MAX_LOCATIONS]

    def _fetcher(self) -> PageFetcher:
        # Search pages are cached for an hour: the same query is not re-requested within a run.
        return make_fetcher(self.cfg.options.get("fetcher", "http"), use_proxy=self.cfg.use_proxy,
                            min_interval=self.opt_float("min_interval", 5.0, floor=5.0), cache_ttl=3600)

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        if not self.take_query_slot():
            return []
        locations = self._locations()
        drafts: dict[str, VacancyDraft] = {}
        failed = 0
        fetcher = self._fetcher()
        try:
            for location in locations:
                params: dict[str, Any] = {"keywords": query.text, "location": location, "start": 0,
                                          "sortBy": "DD"}
                if query.filters.period_days:
                    params["f_TPR"] = f"r{int(query.filters.period_days) * 86400}"
                if query.filters.remote_only:
                    params["f_WT"] = 2
                page = await fetcher.get(SEARCH_URL, params)
                if page.status == 429 or page.looks_blocked:
                    raise SourceError("LinkedIn ограничил частоту запросов (429), попробуйте позже")
                if page.status >= 400:
                    log.warning("linkedin %s: HTTP %s", location, page.status)
                    failed += 1
                    continue
                for d in parse_search_page(page.text):
                    if query.filters.remote_only:  # f_WT=2 returns remote jobs only
                        d = d.model_copy(update={"remote": True})
                    drafts.setdefault(d.external_id, d)
                if len(drafts) >= limit:
                    break
        finally:
            await fetcher.close()
        if locations and failed == len(locations):
            raise SourceError("LinkedIn не отдал результаты поиска")
        return list(drafts.values())[:limit]

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        if not external_id.isdigit():
            return None
        fetcher = self._fetcher()
        try:
            page = await fetcher.get(CARD_URL.format(id=external_id))
        finally:
            await fetcher.close()
        if page.status == 404:
            return None
        if page.status >= 400 or page.looks_blocked:
            raise SourceError(f"LinkedIn не отдал вакансию {external_id} (код {page.status})")
        found = safe_map(lambda h: parse_job_page(h, external_id), [page.text], source=self.title)
        return found[0] if found else None

    def external_id_from_url(self, url: str) -> str | None:
        for rx in _URL_RES:
            m = rx.search(url)
            if m:
                return m.group(1)
        return None
