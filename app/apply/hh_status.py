"""Read the user's applications ("Отклики") from hh.ru with the saved login session.

Read-only: one GET per page of https://hh.ru/applicant/negotiations, no browser needed.
The page markup was NOT verified against the live site during development: the parser
looks for vacancy links and classifies each card by its visible Russian status text, so
it survives most markup changes; the card selector can be overridden in config
([apply.hh_selectors] negotiations_item).
"""

from __future__ import annotations

import asyncio
import re
from http.cookiejar import Cookie
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

from app.apply.base import SessionExpired, SiteApplication, SiteStatus
from app.core.http import make_async_client

PAGE_PATH = "/applicant/negotiations"
MAX_PAGES = 5
PAGE_PAUSE_S = 2.0
_VACANCY_RE = re.compile(r"/vacancy/(\d+)/?$")
SITE_DOMAINS = ("hh.ru", "hh.kz", "hh.uz")
MAX_PAGE_BYTES = 5 * 1024 * 1024
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/140.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Checked in order: the first hit wins (a rejection after an invitation is the latest news).
_STATUS_WORDS: tuple[tuple[SiteStatus, tuple[str, ...]], ...] = (
    ("rejected", ("отказ",)),
    ("invited", ("приглашени", "приглашаем", "собеседовани")),
    ("archived", ("в архиве", "вакансия закрыта", "вакансия удалена")),
    ("applied", ("не просмотрен",)),
    ("viewed", ("просмотрен",)),
)


# Most advanced last: a duplicate card keeps the furthest status.
_ORDER: tuple[SiteStatus, ...] = ("archived", "applied", "viewed", "invited", "rejected")


def classify(text: str) -> SiteStatus | None:
    """Status by the card's visible text; None = no status text (e.g. a recommended vacancy)."""
    low = text.lower().replace("ё", "е")
    for status, words in _STATUS_WORDS:
        if any(w in low for w in words):
            return status
    return None


def _status_text(card: Tag, link: Tag) -> str:
    """Card text without the vacancy title: a title like «…отказ…» must not count."""
    return card.get_text(" ", strip=True).replace(link.get_text(" ", strip=True), " ")


def _vacancy_id(href: str) -> str | None:
    """Only links to hh.ru itself: a link in a card to another site is not an application."""
    parts = urlsplit(urljoin("https://hh.ru/", href))
    host = (parts.hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in SITE_DOMAINS):
        return None
    m = _VACANCY_RE.match(parts.path)
    return m.group(1) if m else None


def _vacancy_ids(node: Tag) -> set[str]:
    return {vid for a in node.select("a[href]") if (vid := _vacancy_id(a["href"]))}


def _card_of(link: Tag, vacancy_id: str) -> Tag:
    """The smallest ancestor of the link that carries a status, without other vacancies in it
    (climbing further could reach page-wide text such as the «Отказ» tab)."""
    card = link
    for _ in range(6):
        parent = card.parent
        if not isinstance(parent, Tag) or parent.name in ("body", "html"):
            break
        if _vacancy_ids(parent) - {vacancy_id}:
            break
        card = parent
        if classify(_status_text(card, link)) is not None:
            break
    return card


def parse_negotiations(html: str, item_selector: str = "") -> list[SiteApplication]:
    soup = BeautifulSoup(html, "html.parser")
    cards: list[tuple[str, Tag, Tag]] = []
    items = soup.select(item_selector) if item_selector else []
    matched = {id(i) for i in items}
    for item in items:
        if any(id(p) in matched for p in item.parents):
            continue  # an inner element that also matches the selector: the outer card counts
        link = next((a for a in item.select("a[href]") if _vacancy_id(a["href"])), None)
        if link is not None:
            cards.append((_vacancy_id(link["href"]), item, link))
    if not items:  # markup changed: fall back to "vacancy link + its surroundings"
        seen: set[str] = set()
        for link in soup.select("a[href]"):
            vid = _vacancy_id(link["href"])
            if vid and vid not in seen:
                seen.add(vid)
                cards.append((vid, _card_of(link, vid), link))
    result: dict[str, SiteApplication] = {}
    for vid, card, link in cards:
        status = classify(_status_text(card, link))
        if status is None:  # no status text: a recommendation or a banner, not an application
            continue
        prev = result.get(vid)
        if prev is not None and _ORDER.index(prev.status) >= _ORDER.index(status):
            continue
        title = " ".join(link.get_text(" ", strip=True).split())[:300]
        company_node = card.select_one('[data-qa*="company"], [data-qa*="employer"]')
        company = " ".join(company_node.get_text(" ", strip=True).split())[:300] if company_node else ""
        result[vid] = SiteApplication(external_id=vid, status=status, title=title or f"Вакансия {vid}",
                                      company=company or None, url=f"https://hh.ru/vacancy/{vid}")
    return list(result.values())


def _cookies(state: dict[str, Any]) -> httpx.Cookies:
    jar = httpx.Cookies()
    for c in state.get("cookies") or []:
        if isinstance(c, dict) and c.get("name") and c.get("value") is not None:
            # Only for https hh.ru: a plain-http hop must not carry the session.
            cookie = Cookie(0, str(c["name"]), str(c["value"]), None, False, str(c.get("domain", "")),
                            True, str(c.get("domain", "")).startswith("."), str(c.get("path") or "/"), True,
                            True, None, False, None, None, {})
            jar.jar.set_cookie(cookie)
    return jar


async def _get_page(client: httpx.AsyncClient, url: str, page: int) -> tuple[httpx.Response, str]:
    """GET with a size cap."""
    async with client.stream("GET", url, params={"page": page}) as resp:
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_PAGE_BYTES:
                raise httpx.DecodingError("hh.ru page is too large")
            chunks.append(chunk)
        return resp, b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")


def _logged_out(resp: httpx.Response, text: str, login_selector: str) -> bool:
    if any(m in str(resp.url) for m in ("/account/login", "/account/signup")):
        return True
    if resp.is_redirect and "/account/" in resp.headers.get("location", ""):
        return True
    return bool(login_selector) and BeautifulSoup(text, "html.parser").select_one(login_selector) is not None


async def fetch_negotiations(state: dict[str, Any], *, base_url: str, item_selector: str, login_selector: str,
                             use_proxy: bool, client: httpx.AsyncClient | None = None) -> list[SiteApplication]:
    own = client is None
    client = client or make_async_client(use_proxy=use_proxy, headers=BROWSER_HEADERS, timeout=30.0)
    client.cookies = _cookies(state)
    found: dict[str, SiteApplication] = {}
    try:
        for page in range(MAX_PAGES):
            if page:
                await asyncio.sleep(PAGE_PAUSE_S)
            resp, text = await _get_page(client, f"{base_url}{PAGE_PATH}", page)
            if _logged_out(resp, text, login_selector):
                raise SessionExpired("вход в hh.ru истёк — войдите заново")
            resp.raise_for_status()
            items = [i for i in parse_negotiations(text, item_selector) if i.external_id not in found]
            if not items:
                break
            found.update((i.external_id, i) for i in items)
    finally:
        if own:
            await client.aclose()
    return list(found.values())
