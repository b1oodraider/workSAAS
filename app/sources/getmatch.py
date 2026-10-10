"""getmatch (getmatch.ru) — Russian IT job board where every vacancy shows a salary range.

API: https://getmatch.ru/api/offers?q=<text>&pa=all&limit=N&offset=0 (the JSON the site's own
search page uses; no key; items carry a short teaser) and /api/offers/{id} for the full vacancy. It is not a documented public
API and getmatch's robots.txt disallows /api/ for crawlers, so the source is off by default:
enable it in config consciously. Requests are spaced >= 1 s apart and cached for an hour.
Link back to the vacancy URL.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft, as_dict, as_str, safe_map
from app.sources.boards import pick
from app.sources.jsonld import parse_date
from app.sources.web import fetch_json

SITE = "https://getmatch.ru"
API = f"{SITE}/api/offers"
MAX_PAGE = 100
_URL_RE = re.compile(r"getmatch\.ru/vacancies/(\d+)")
_FORMATS = {"remote": "удалённо", "hybrid": "гибрид", "office": "офис", "relocation_company": "релокация"}
_CURRENCY = {"RUB": "RUR", "RUR": "RUR"}


def _num(value: Any) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    if not item.get("id") or not as_str(item.get("position")):
        return None
    if item.get("is_active") is False:
        return None
    places = [as_dict(x) for x in item.get("location_items") or [] if not as_dict(x).get("exclude")]
    formats = {str(p.get("format")) for p in places}
    location = "; ".join(
        f"{p['label']} ({_FORMATS.get(str(p.get('format')), p.get('format'))})" if p.get("format") else str(p["label"])
        for p in places if p.get("label")) or None

    sal_from = sal_to = currency = gross = None
    if not item.get("salary_hidden"):
        sal_from, sal_to = _num(item.get("salary_display_from")), _num(item.get("salary_display_to"))
        if sal_from or sal_to:
            raw_currency = str(item.get("salary_currency") or "").upper()
            currency = _CURRENCY.get(raw_currency, raw_currency or None)
            taxes = item.get("salary_taxes")
            gross = True if taxes == "gross" else (False if taxes == "net" else None)

    # Search results carry a short teaser (offer_description); /api/offers/{id} has the full text.
    full = item.get("description") or item.get("description_html")
    desc = html_to_text(full or item.get("offer_description") or "")
    english = as_str(item.get("english_level"))
    if english:
        desc = f"Английский: {english}\n\n{desc}"
    url = as_str(item.get("url"))
    return VacancyDraft(
        source="getmatch",
        external_id=str(item["id"]),
        title=as_str(item.get("position"))[:300],
        url=f"{SITE}{url}" if url and url.startswith("/") else url,
        company=as_str(item.get("company")),
        location=location,
        salary_from=sal_from,
        salary_to=sal_to,
        currency=currency,
        salary_gross=gross,
        remote=("remote" in formats) if formats else None,
        skills=[s for s in (as_str(x) for x in item.get("skills_objects") or []) if s][:20],
        description=desc,
        is_partial=not full,
        published_at=parse_date(item.get("published_at")),
    )


class GetmatchSource(JobSource):
    name = "getmatch"
    title = "getmatch"
    hint = "IT-вакансии с зарплатами"
    enabled_by_default = False

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        # Ask for more than ``limit``: the local filter below drops loose server matches.
        params = {"q": query.text, "pa": "all", "limit": min(max(limit * 2, 20), MAX_PAGE), "offset": 0}
        data = await fetch_json(API, params=params, use_proxy=self.cfg.use_proxy, source=self.title,
                                min_interval=1.0, cache_ttl=3600)
        offers = as_dict(data).get("offers")
        if not isinstance(offers, list):
            raise SourceError(f"{self.title}: неожиданный ответ")
        drafts = safe_map(item_to_draft, offers, source=self.title)
        # Server search is fuzzy (hiring events etc. match everything): keep query-word matches.
        return pick(drafts, query, limit)

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        if not external_id.isdigit():
            return None
        data = await fetch_json(f"{API}/{external_id}", use_proxy=self.cfg.use_proxy, source=self.title,
                                min_interval=1.0, cache_ttl=3600)
        found = safe_map(item_to_draft, [data], source=self.title)
        return found[0] if found else None

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
