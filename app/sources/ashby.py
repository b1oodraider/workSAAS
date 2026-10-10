"""Ashby job boards — employers' own career pages (ATS), public JSON API without a key.

API: https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true (all listed
jobs of one company, with salary ranges when the employer publishes them). The Posting API is
public and meant for career sites; there is no search and no single-job endpoint, so each
board is fetched whole at most once an hour (shared cache) with >= 1 s between requests, and
filtered locally. Link back to ``jobUrl``.

Options:
  companies: list of Ashby board names (the part after jobs.ashbyhq.com/), e.g. ["linear", "supabase"].
             Empty -> the source searches nothing.
  match_all_words: if true, every query word must appear (default: any word)
"""

from __future__ import annotations

import re
from typing import Any

from app.core.text import html_to_text
from app.sources.base import SourceError, VacancyDraft, as_dict, as_str, safe_map
from app.sources.boards import TOKEN_RE, BoardSource
from app.sources.jsonld import parse_date
from app.sources.salary import known_currency
from app.sources.web import fetch_json

API = "https://api.ashbyhq.com/posting-api/job-board"
_URL_RE = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{36})")
# Salary component intervals stored as numbers; hourly/other rates stay only in the text.
_INTERVALS = {"1 YEAR": "в год", "1 MONTH": "в месяц"}
_WORKPLACE = {"remote": True, "onsite": False, "hybrid": False}
_EMPLOYMENT = {"FullTime": "Полная занятость", "PartTime": "Частичная занятость", "Contract": "Контракт",
               "Intern": "Стажировка", "Temporary": "Временная работа"}


def _salary(comp: dict[str, Any]) -> tuple[int | None, int | None, str | None, str | None]:
    """compensation -> (from, to, currency, period) of the first yearly/monthly Salary component."""
    components = comp.get("summaryComponents") or [
        c for tier in comp.get("compensationTiers") or [] for c in as_dict(tier).get("components") or []]
    for c in components:
        c = as_dict(c)
        period = _INTERVALS.get(str(c.get("interval")))
        if c.get("compensationType") != "Salary" or not period:
            continue
        sal_from = int(float(c["minValue"])) if c.get("minValue") else None
        sal_to = int(float(c["maxValue"])) if c.get("maxValue") else None
        currency = known_currency(as_str(c.get("currencyCode")))
        # Numbers without a known currency would mislead the ranking: they stay in the text only.
        if (sal_from or sal_to) and currency:
            return sal_from, sal_to, currency, period
    return None, None, None, None


def item_to_draft(item: dict[str, Any], org: str) -> VacancyDraft | None:
    if not item.get("id") or not as_str(item.get("title")) or item.get("isListed") is False:
        return None
    locations = [as_str(item.get("location"))] + [
        as_str(as_dict(x).get("location")) for x in item.get("secondaryLocations") or []]
    location = "; ".join(x for x in locations if x) or None

    desc = html_to_text(item.get("descriptionHtml") or "") or html_to_text(item.get("descriptionPlain") or "")
    team = " / ".join(x for x in (as_str(item.get("department")), as_str(item.get("team"))) if x)
    if team:
        desc = f"Команда: {team}\n\n{desc}"
    comp = as_dict(item.get("compensation"))
    sal_from, sal_to, currency, period = _salary(comp)
    summary = as_str(comp.get("compensationTierSummary")) or as_str(comp.get("scrapeableCompensationSalarySummary"))
    if summary:
        desc = f"Компенсация{' ' + period if period else ''}: {summary}\n{desc}"

    workplace = str(item.get("workplaceType") or "").lower()
    remote = True if item.get("isRemote") is True else _WORKPLACE.get(workplace)
    if remote is None and location and "remote" in location.lower():
        remote = True
    employment = as_str(item.get("employmentType"))
    return VacancyDraft(
        source="ashby",
        external_id=f"{org}/{item['id']}",
        title=as_str(item.get("title"))[:300],
        url=item.get("jobUrl"),
        company=org,
        location=location,
        salary_from=sal_from,
        salary_to=sal_to,
        currency=currency,
        remote=remote,
        employment=_EMPLOYMENT.get(employment or "", employment),
        description=desc,
        published_at=parse_date(item.get("publishedAt")),
    )


# Verified 2026-10-08: boards that answer and hire engineers.
DEFAULT_COMPANIES = ("openai", "clickhouse", "elevenlabs", "supabase", "sentry", "ramp", "notion", "linear",
                     "n8n", "synthesia")


class AshbySource(BoardSource):
    name = "ashby"
    title = "Ashby"
    hint = "карьерные страницы компаний (Ashby): OpenAI, Notion, Linear…; список меняется в конфиге"
    default_companies = DEFAULT_COMPANIES

    async def load_board(self, org: str) -> list[VacancyDraft]:
        data = await fetch_json(f"{API}/{org}", params={"includeCompensation": "true"},
                                use_proxy=self.cfg.use_proxy, source=f"{self.title} {org}", min_interval=1.0,
                                cache_ttl=3600)
        jobs = as_dict(data).get("jobs")
        if not isinstance(jobs, list):
            raise SourceError(f"{self.title} {org}: неожиданный ответ")
        return safe_map(lambda item: item_to_draft(item, org), jobs, source=self.title)

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        # No single-job endpoint: read the (cached) board and pick the job.
        org, _, job_id = external_id.partition("/")
        if not TOKEN_RE.match(org) or not job_id:
            return None
        return next((d for d in await self._board(org) if d.external_id == external_id), None)

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return f"{m.group(1)}/{m.group(2).lower()}" if m else None
