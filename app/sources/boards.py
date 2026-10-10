"""Helpers shared by job-board sources that read many boards/feeds and filter locally.

Used by the employer ATS sources (Greenhouse, Lever, Ashby: one public board per company)
and by RSS boards (We Work Remotely, Djinni). None of these APIs has a usable full-text
search, so every board is fetched whole (cached for an hour) and filtered by query words.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from app.sources.base import (
    JobSource,
    SearchQuery,
    SourceError,
    VacancyDraft,
    matches_query,
    query_words,
    relevance_key,
)
from app.sources.rss import _dt as rss_date
from app.sources.web import make_fetcher

log = logging.getLogger(__name__)

# Board tokens go into API URL paths: letters, digits, dot, dash, underscore only.
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
MAX_COMPANIES = 50
# Description prefix used for local matching (long ATS texts mention everything).
MATCH_DESCRIPTION_CHARS = 3000


def company_tokens(options: dict[str, Any], default: Iterable[str] = ()) -> list[str]:
    """``companies`` option -> validated, de-duplicated board tokens (list or "a, b" string).
    Without the option the source's default list is used; ``companies = []`` turns it off."""
    raw = options["companies"] if "companies" in options else list(default)
    raw = raw or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, (list, tuple)):
        log.warning("ignored invalid companies option %r", raw)
        raw = []
    tokens: list[str] = []
    for value in raw:
        token = str(value).strip()
        if not TOKEN_RE.match(token):
            log.warning("ignored invalid company token %r", token)
        elif token not in tokens:
            tokens.append(token)
    return tokens[:MAX_COMPANIES]


async def collect(keys: Iterable[str], load: Callable[[str], Awaitable[list[VacancyDraft]]], *,
                  source: str) -> list[VacancyDraft]:
    """Load boards one by one (the shared per-host rate limiter spaces the requests).

    A failing board is logged and skipped; only if every board fails is the error raised.
    """
    keys = list(keys)
    drafts: list[VacancyDraft] = []
    errors: list[SourceError] = []
    for key in keys:
        try:
            drafts += await load(key)
        except SourceError as exc:
            log.warning("%s: %s skipped: %s", source, key, exc)
            errors.append(exc)
    if keys and len(errors) == len(keys):
        raise SourceError(f"{source}: ни одна страница не загрузилась ({errors[0]})")
    return drafts


def pick(drafts: list[VacancyDraft], query: SearchQuery, limit: int, *,
         all_words: bool = False) -> list[VacancyDraft]:
    """Local search: query words in title/description, dedup by id. Ranked by how many query
    words the title contains, then the body, then by date."""
    words = query_words(query.text)
    seen: set[str] = set()
    found: list[tuple[tuple, VacancyDraft]] = []
    for d in drafts:
        if d.external_id in seen:
            continue
        body = f"{d.company or ''}\n{' '.join(d.skills)}\n{d.description[:MATCH_DESCRIPTION_CHARS]}"
        if matches_query(f"{d.title}\n{body}", query.text, all_words=all_words):
            seen.add(d.external_id)
            found.append((relevance_key(d.title, body, d.published_at, words), d))
    found.sort(key=lambda item: item[0], reverse=True)
    return [d for _, d in found[:limit]]


class BoardSource(JobSource):
    """Employer ATS source (Greenhouse, Lever, Ashby): one public board per company, no search.

    Subclasses implement ``load_board``. Every board is fetched and parsed at most once per
    instance (= one search run, ~8 queries) and a failing board is not retried within the run.
    """

    default_companies: tuple[str, ...] = ()
    checked_by_default = False

    @abstractmethod
    async def load_board(self, token: str) -> list[VacancyDraft]: ...

    async def _board(self, token: str) -> list[VacancyDraft]:
        return await self.memoized(("board", token), lambda: self.load_board(token))

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        tokens = company_tokens(self.cfg.options, self.default_companies)
        if not tokens:
            return []
        drafts = await collect(tokens, self._board, source=self.title)
        return pick(drafts, query, limit, all_words=bool(self.cfg.options.get("match_all_words")))


async def fetch_feed(url: str, *, params: dict[str, Any] | None = None, use_proxy: bool = False,
                     source: str = "") -> str:
    """GET an RSS feed politely (shared rate limiter, 1 h cache); errors become SourceError."""
    fetcher = make_fetcher("http", use_proxy=use_proxy, min_interval=1.0, cache_ttl=3600)
    try:
        page = await fetcher.get(url, params)
    finally:
        await fetcher.close()
    if page.status >= 400:
        raise SourceError(f"{source or url} ответил {page.status}")
    return page.text


async def fetch_page(url: str, *, use_proxy: bool = False, source: str = "") -> str | None:
    """Vacancy HTML page (for JSON-LD); None when the vacancy is gone."""
    fetcher = make_fetcher("http", use_proxy=use_proxy, min_interval=1.0)
    try:
        page = await fetcher.get(url)
    finally:
        await fetcher.close()
    if page.status == 404:
        return None
    if page.status >= 400 or page.looks_blocked:
        raise SourceError(f"{source or url} не отдал вакансию (код {page.status})")
    return page.text


def rss_items(xml_text: str, *, source: str = "") -> list[dict[str, Any]]:
    """RSS 2.0 <item>s as dicts: first text of every plain child tag, all non-empty
    <category> values under ``categories`` and ``published`` (naive datetime) from pubDate."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceError(f"{source}: некорректный RSS: {exc}") from exc
    items = []
    for item in root.iter("item"):
        data: dict[str, Any] = {"categories": []}
        for child in item:
            if not isinstance(child.tag, str) or child.tag.startswith("{"):
                continue  # namespaced extras (media:content etc.)
            text = (child.text or "").strip()
            if child.tag == "category":
                if text:
                    data["categories"].append(text)
            else:
                data.setdefault(child.tag, text)
        data["published"] = rss_date(data.get("pubDate"))
        items.append(data)
    return items
