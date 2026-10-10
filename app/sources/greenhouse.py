"""Greenhouse job boards — employers' own career pages (ATS), public JSON API without a key.

API: https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true (all open jobs of
one company; ``content`` is HTML-escaped HTML) and .../jobs/{id} for one job.
The Job Board API is public and meant for embedding; there is no search, so each board is
fetched whole at most once an hour (shared cache) with >= 1 s between requests, and filtered
locally. Link back to ``absolute_url``.

Options:
  companies: list of board tokens (the part after greenhouse.io/), e.g. ["gitlab", "elastic"].
             Empty -> the source searches nothing.
  match_all_words: if true, every query word must appear (default: any word)
"""

from __future__ import annotations

import html
import re
from typing import Any

from app.core.text import html_to_text
from app.sources.base import SourceError, VacancyDraft, as_dict, as_str, safe_map
from app.sources.boards import TOKEN_RE, BoardSource
from app.sources.jsonld import parse_date
from app.sources.web import fetch_json

API = "https://boards-api.greenhouse.io/v1/boards"
_URL_RE = re.compile(r"(?:job-)?boards\.greenhouse\.io/([A-Za-z0-9_.-]+)/jobs/(\d+)")


def item_to_draft(item: dict[str, Any], token: str) -> VacancyDraft | None:
    if not item.get("id") or not as_str(item.get("title")):
        return None
    location = as_str(item.get("location"))
    departments = [as_str(d) for d in item.get("departments") or []]
    desc = html_to_text(html.unescape(item.get("content") or ""))
    team = ", ".join(d for d in departments if d)
    if team:
        desc = f"Отдел: {team}\n\n{desc}"
    return VacancyDraft(
        source="greenhouse",
        external_id=f"{token}/{item['id']}",
        title=as_str(item.get("title"))[:300],
        url=item.get("absolute_url"),
        company=as_str(item.get("company_name")) or token,
        location=location,
        remote=True if location and "remote" in location.lower() else None,
        description=desc,
        published_at=parse_date(item.get("first_published") or item.get("updated_at")),
    )


# Verified 2026-10-08: boards that answer and hire engineers remotely / in Europe.
DEFAULT_COMPANIES = ("gitlab", "elastic", "grafanalabs", "reddit", "canonical", "datadog", "celonis", "twilio",
                     "doctolib", "anthropic", "databricks", "stripe", "mongodb", "n26")


class GreenhouseSource(BoardSource):
    name = "greenhouse"
    title = "Greenhouse"
    hint = "карьерные страницы компаний (Greenhouse): GitLab, Stripe, Datadog…; список меняется в конфиге"
    default_companies = DEFAULT_COMPANIES

    async def load_board(self, token: str) -> list[VacancyDraft]:
        data = await fetch_json(f"{API}/{token}/jobs", params={"content": "true"}, use_proxy=self.cfg.use_proxy,
                                source=f"{self.title} {token}", min_interval=1.0, cache_ttl=3600)
        jobs = as_dict(data).get("jobs")
        if not isinstance(jobs, list):
            raise SourceError(f"{self.title} {token}: неожиданный ответ")
        return safe_map(lambda item: item_to_draft(item, token), jobs, source=self.title)

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        token, _, job_id = external_id.partition("/")
        if not TOKEN_RE.match(token) or not job_id.isdigit():
            return None
        data = await fetch_json(f"{API}/{token}/jobs/{job_id}", use_proxy=self.cfg.use_proxy,
                                source=self.title, min_interval=1.0, cache_ttl=3600)
        found = safe_map(lambda item: item_to_draft(item, token), [data], source=self.title)
        return found[0] if found else None

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return f"{m.group(1)}/{m.group(2)}" if m else None
