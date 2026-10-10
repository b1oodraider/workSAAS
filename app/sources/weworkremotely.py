"""We Work Remotely (weworkremotely.com) — remote-only job board, public RSS per category.

Feeds: https://weworkremotely.com/categories/<category>.rss (25-50 latest jobs each, item title
is "Company: Role", <region> is the candidate location). robots.txt allows these pages;
the feeds are fetched at most once an hour (shared cache) with >= 1 s between requests and
filtered locally. Link back to the job URL. A job page carries schema.org JSON-LD (``fetch``).

Options:
  feeds: list of feed URLs (default: programming, back-end, front-end, full-stack, devops/sysadmin)
  match_all_words: if true, every query word must appear (default: any word)
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from bs4 import BeautifulSoup

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, VacancyDraft, safe_map
from app.sources.boards import collect, fetch_feed, fetch_page, pick, rss_items
from app.sources.jsonld import draft_from_job_posting, walk

SITE = "https://weworkremotely.com"
DEFAULT_FEEDS = [
    f"{SITE}/categories/remote-programming-jobs.rss",
    f"{SITE}/categories/remote-back-end-programming-jobs.rss",
    f"{SITE}/categories/remote-front-end-programming-jobs.rss",
    f"{SITE}/categories/remote-full-stack-programming-jobs.rss",
    f"{SITE}/categories/remote-devops-sysadmin-jobs.rss",
]
_URL_RE = re.compile(r"weworkremotely\.com/remote-jobs/([a-z0-9-]+)")


def split_title(title: str) -> tuple[str | None, str]:
    """'Acme: Senior Python Engineer' -> ('Acme', 'Senior Python Engineer')."""
    company, sep, role = title.partition(": ")
    if sep and company.strip() and role.strip():
        return company.strip(), role.strip()
    return None, title.strip()


def parse_job_page(page: str, external_id: str, url: str) -> VacancyDraft | None:
    """Job page -> draft from its JSON-LD. WWR's JSON-LD has raw newlines inside strings
    (invalid strict JSON) and an HTML-escaped description, so it is parsed leniently here."""
    for tag in BeautifulSoup(page, "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text(), strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
        for jp in walk(data):
            if jp.get("@type") == "JobPosting":
                if isinstance(jp.get("description"), str):
                    jp = {**jp, "description": html.unescape(jp["description"])}
                found = safe_map(lambda j: draft_from_job_posting(j, source="weworkremotely",
                                                                  external_id=external_id, url=url), [jp])
                if found:
                    draft = found[0]
                    draft.remote = True
                    # WWR fills baseSalary with zeros when no salary is given.
                    draft.salary_from = draft.salary_from or None
                    draft.salary_to = draft.salary_to or None
                    if not (draft.salary_from or draft.salary_to):
                        draft.currency = None
                    return draft
    return None


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    link = item.get("link") or item.get("guid") or ""
    m = _URL_RE.search(link)
    title = html_to_text(item.get("title") or "")
    if not m or not title:
        return None
    company, role = split_title(title)
    desc = html_to_text(item.get("description") or "")
    categories = item.get("categories") or []
    if categories:
        desc = f"Категория: {', '.join(categories)}\n\n{desc}"
    return VacancyDraft(
        source="weworkremotely",
        external_id=m.group(1),
        title=role[:300],
        url=link,
        company=company,
        location=item.get("region") or None,
        remote=True,
        description=desc,
        published_at=item.get("published"),
    )


class WeWorkRemotelySource(JobSource):
    name = "weworkremotely"
    title = "We Work Remotely"
    hint = "удалёнка за рубежом, англ."
    checked_by_default = False

    async def _feed(self, url: str) -> list[VacancyDraft]:
        async def load() -> list[VacancyDraft]:
            text = await fetch_feed(url, use_proxy=self.cfg.use_proxy, source=self.title)
            return safe_map(item_to_draft, rss_items(text, source=self.title), source=self.title)
        return await self.memoized(("feed", url), load)  # parsed once per run, not per query

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        feeds = self.opt_list("feeds") or DEFAULT_FEEDS
        drafts = await collect(feeds, self._feed, source=self.title)
        return pick(drafts, query, limit, all_words=bool(self.cfg.options.get("match_all_words")))

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        if not re.fullmatch(r"[a-z0-9-]+", external_id):
            return None
        url = f"{SITE}/remote-jobs/{external_id}"
        page = await fetch_page(url, use_proxy=self.cfg.use_proxy, source=self.title)
        return parse_job_page(page, external_id, url) if page is not None else None

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
