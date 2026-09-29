"""Generic RSS 2.0 / Atom source. Many job boards and Telegram mirrors expose feeds.

Options:
  feeds: list of feed URLs
  match_all_words: if true, every query word must appear (default: any word)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx

from app.core.http import make_async_client
from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft, matches_query

log = logging.getLogger(__name__)

_ATOM = "{http://www.w3.org/2005/Atom}"


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).replace(tzinfo=None)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def parse_feed(xml_text: str, feed_url: str) -> list[VacancyDraft]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceError(f"Некорректный RSS {feed_url}: {exc}") from exc
    drafts: list[VacancyDraft] = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        drafts.append(_draft(guid, item.findtext("title"), link,
                             item.findtext("description"), _dt(item.findtext("pubDate"))))
    for entry in root.iter(f"{_ATOM}entry"):
        link_el = entry.find(f"{_ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        guid = (entry.findtext(f"{_ATOM}id") or link).strip()
        body = entry.findtext(f"{_ATOM}content") or entry.findtext(f"{_ATOM}summary")
        drafts.append(_draft(guid, entry.findtext(f"{_ATOM}title"), link, body,
                             _dt(entry.findtext(f"{_ATOM}updated"))))
    return drafts


def _draft(guid: str, title: str | None, link: str, body: str | None,
           published: datetime | None) -> VacancyDraft:
    return VacancyDraft(
        source="rss",
        external_id=hashlib.sha1(guid.encode()).hexdigest()[:32],
        title=html_to_text(title or "")[:300] or "(без названия)",
        url=link or None,
        description=html_to_text(body or ""),
        published_at=published,
    )


class RSSSource(JobSource):
    name = "rss"
    trusted = False
    enabled_by_default = False
    title = "RSS-ленты"

    async def _load(self, client: httpx.AsyncClient, url: str) -> list[VacancyDraft]:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceError(f"RSS {url} недоступен: {exc}") from exc
        return parse_feed(resp.text, url)

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        feeds: list[str] = list(self.cfg.options.get("feeds") or [])
        if not feeds:
            return []
        async with make_async_client(use_proxy=self.cfg.use_proxy) as client:
            results = await asyncio.gather(*(self._load(client, u) for u in feeds),
                                           return_exceptions=True)
        failed = [r for r in results if isinstance(r, Exception)]
        for exc in failed:
            log.warning("rss feed failed: %s", exc)
        if failed and len(failed) == len(results):
            raise SourceError(f"Все RSS-ленты недоступны: {failed[0]}")
        drafts = [d for r in results if isinstance(r, list) for d in r]
        need_all = bool(self.cfg.options.get("match_all_words"))
        found = [d for d in drafts
                 if matches_query(f"{d.title}\n{d.description}", query.text, all_words=need_all)]
        found.sort(key=lambda d: d.published_at or datetime.min, reverse=True)
        return found[:limit]
