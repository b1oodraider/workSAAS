"""Lever job boards — employers' own career pages (ATS), public JSON API without a key.

API: https://api.lever.co/v0/postings/{company}?mode=json (all published postings of one
company) and .../postings/{company}/{id}?mode=json for one posting. The Postings API is
public and meant for career sites; there is no search, so each board is fetched whole at
most once an hour (shared cache) with >= 1 s between requests, and filtered locally.
Link back to ``hostedUrl``. An unknown company returns 404 or an empty list.

Options:
  companies: list of Lever site names (the part after jobs.lever.co/), e.g. ["palantir", "spotify"].
             Empty -> the source searches nothing.
  match_all_words: if true, every query word must appear (default: any word)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.core.text import html_to_text
from app.sources.base import SourceError, VacancyDraft, as_dict, as_str, safe_map
from app.sources.boards import TOKEN_RE, BoardSource
from app.sources.salary import known_currency
from app.sources.web import fetch_json

API = "https://api.lever.co/v0/postings"
_URL_RE = re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{36})")
# salaryRange.interval values; only these are stored as numbers (hourly rates would mislead).
_INTERVALS = {"per-year-salary": "в год", "per-month-salary": "в месяц"}
_WORKPLACE = {"remote": True, "onsite": False, "hybrid": False}


def _description(item: dict[str, Any]) -> str:
    parts = [html_to_text(item.get("description") or "") or html_to_text(item.get("descriptionPlain") or "")]
    for block in item.get("lists") or []:
        block = as_dict(block)
        body = html_to_text(block.get("content") or "")
        if body:
            parts.append(f"{html_to_text(block.get('text') or '')}\n{body}".strip())
    parts.append(html_to_text(item.get("additional") or "") or html_to_text(item.get("additionalPlain") or ""))
    return "\n\n".join(p for p in parts if p)


def _published(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(tzinfo=None)


def item_to_draft(item: dict[str, Any], company: str) -> VacancyDraft | None:
    if not item.get("id") or not as_str(item.get("text")):
        return None
    cats = as_dict(item.get("categories"))
    locations = [str(x) for x in cats.get("allLocations") or [] if x] or [as_str(cats.get("location")) or ""]
    location = "; ".join(x for x in locations if x) or None
    desc = _description(item)
    team = as_str(cats.get("team"))
    if team:
        desc = f"Команда: {team}\n\n{desc}"

    sal_from = sal_to = currency = None
    salary = as_dict(item.get("salaryRange"))
    period = _INTERVALS.get(str(salary.get("interval")))
    if salary and period:
        sal_from = int(float(salary["min"])) if salary.get("min") else None
        sal_to = int(float(salary["max"])) if salary.get("max") else None
        if sal_from or sal_to:
            raw_currency = as_str(salary.get("currency"))
            desc = f"Зарплата {period}: {sal_from or ''}–{sal_to or ''} {raw_currency or ''}".rstrip() + f"\n{desc}"
            currency = known_currency(raw_currency)
            if not currency:  # numbers without a currency would mislead: keep them in the text only
                sal_from = sal_to = None

    workplace = str(item.get("workplaceType") or "").lower()
    return VacancyDraft(
        source="lever",
        external_id=f"{company}/{item['id']}",
        title=as_str(item.get("text"))[:300],
        url=item.get("hostedUrl"),
        company=company,
        location=location,
        salary_from=sal_from,
        salary_to=sal_to,
        currency=currency,
        remote=_WORKPLACE.get(workplace),
        employment=as_str(cats.get("commitment")),
        description=desc,
        published_at=_published(item.get("createdAt")),
    )


# Verified 2026-10-08: boards that answer and hire engineers.
DEFAULT_COMPANIES = ("palantir", "spotify", "binance", "veeva", "jumpcloud", "zopa", "dnb")


class LeverSource(BoardSource):
    name = "lever"
    title = "Lever"
    hint = "карьерные страницы компаний (Lever): Palantir, Spotify…; список меняется в конфиге"
    default_companies = DEFAULT_COMPANIES

    async def load_board(self, company: str) -> list[VacancyDraft]:
        data = await fetch_json(f"{API}/{company}", params={"mode": "json"}, use_proxy=self.cfg.use_proxy,
                                source=f"{self.title} {company}", min_interval=1.0, cache_ttl=3600)
        if not isinstance(data, list):
            raise SourceError(f"{self.title} {company}: неожиданный ответ")
        return safe_map(lambda item: item_to_draft(item, company), data, source=self.title)

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        company, _, posting_id = external_id.partition("/")
        if not TOKEN_RE.match(company) or not re.fullmatch(r"[0-9a-fA-F-]{36}", posting_id):
            return None
        data = await fetch_json(f"{API}/{company}/{posting_id}", params={"mode": "json"},
                                use_proxy=self.cfg.use_proxy, source=self.title, min_interval=1.0, cache_ttl=3600)
        found = safe_map(lambda item: item_to_draft(item, company), [data], source=self.title)
        return found[0] if found else None

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return f"{m.group(1)}/{m.group(2).lower()}" if m else None
