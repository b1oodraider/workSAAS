"""Авито Работа (avito.ru) — job ads, read from the search page's embedded state. SEARCH ONLY.

Search:  GET https://www.avito.ru/<location>/vakansii?q=<q>[&p=N] (~2 MB HTML). The page embeds
         ``<script type="mime/invalid" data-mfe-state="true">`` with JSON
         ``loaderData.data.catalog.items[]`` (type "item"): id, title, urlPath, location,
         priceDetailed.fullString ("150 000 — 180 000 ₽ на руки"), sortTimeStamp (ms),
         iva.DescriptionStep (description cut to ~250 chars), iva.UserInfoStep (employer name).
         Items with ``isSimilarToSearch`` ("Похоже на ваш поиск") are not matches and are skipped.

WARNING: Avito has aggressive anti-bot protection and its terms forbid automated collection.
The search page answered 200 to plain HTTP on 2026-10-08, but the very next request (a vacancy
card) got HTTP 439 "Доступ ограничен: проверка безопасности". Hence: opt-in only
(``enabled_by_default = False``), one page per query by default, slow rate limit, and no
``fetch()`` — cards are not read, drafts stay partial (snippet only); the user can paste the
full text manually. Expect SourceError (blocked) from time to time.

Markup verified live on 2026-10-08 (search page only).

Options:
  location      Avito location slug in the URL (default "all" — вся Россия; e.g. "moskva")
  pages         max result pages per query (1..5, default 1)
  fetcher       "http" | "auto" | "browser" (default "http"; "auto" escapes the security check
                with a headless browser and must be chosen explicitly)
  min_interval  seconds between requests (default 5.0, lower values are raised to 5)
  max_queries   only the first N queries of a search run are sent (default 3)
  include_similar  also keep "Похоже на ваш поиск" items (default false)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft, as_dict, as_str, safe_map
from app.sources.salary import parse_salary_text
from app.sources.web import PageFetcher, make_fetcher

SITE = "https://www.avito.ru"
NBSP = chr(0xA0)
_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,60}$")


def _steps(item: dict[str, Any], name: str) -> list[dict[str, Any]]:
    steps = as_dict(item.get("iva")).get(name)
    return [s for s in steps or [] if isinstance(s, dict)]


def _snippet(item: dict[str, Any]) -> str:
    for step in _steps(item, "DescriptionStep"):
        text = as_str(as_dict(step.get("payload")).get("description"))
        if text:
            return text
    return ""


def _employer(item: dict[str, Any]) -> str | None:
    for step in _steps(item, "UserInfoStep"):
        if as_dict(step.get("componentData")).get("component") == "seller-info":
            return as_str(as_dict(as_dict(step.get("payload")).get("profile")).get("title"))
    return None


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    if item.get("type", "item") != "item" or not item.get("id") or not as_str(item.get("title")):
        return None
    path = str(item.get("urlPath") or "").split("?", 1)[0]
    if not path.startswith("/"):
        return None
    price = as_dict(item.get("priceDetailed"))
    salary = (as_str(price.get("fullString")) or "").replace(NBSP, " ") if price.get("hasValue") else ""
    sal_from, sal_to, currency, gross = parse_salary_text(salary)
    location = as_str(as_dict(item.get("geo")).get("formattedAddress")) or as_str(item.get("location"))
    snippet = _snippet(item).replace(NBSP, " ")
    title = as_str(item.get("title")) or ""
    low = f"{title}\n{snippet}".lower()
    return VacancyDraft(
        source="avito_jobs",
        external_id=str(item["id"]),
        title=title[:300],
        url=SITE + path,
        company=_employer(item),
        location=location,
        salary_from=sal_from,
        salary_to=sal_to,
        currency=currency,
        salary_gross=gross,
        remote=True if ("удалённ" in low or "удаленн" in low) else None,
        description=(f"Зарплата: {salary}\n\n{snippet}" if salary else snippet).strip(),
        is_partial=True,
        published_at=_timestamp(item.get("sortTimeStamp")),
    )


def _state(soup: BeautifulSoup) -> Any:
    for tag in soup.find_all("script", attrs={"data-mfe-state": True}):
        text = tag.string or tag.get_text()
        if "catalog" not in text:
            continue
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def parse_search_page(html: str, *, include_similar: bool = False) -> list[VacancyDraft]:
    state = _state(BeautifulSoup(html, "html.parser"))
    catalog = as_dict(as_dict(as_dict(as_dict(state).get("loaderData")).get("data")).get("catalog"))
    items = [i for i in catalog.get("items") or [] if isinstance(i, dict)
             and i.get("type") == "item" and (include_similar or not i.get("isSimilarToSearch"))]
    drafts: dict[str, VacancyDraft] = {}
    for d in safe_map(item_to_draft, items, source="Авито"):
        drafts.setdefault(d.external_id, d)
    return list(drafts.values())


class AvitoJobsSource(JobSource):
    name = "avito_jobs"
    title = "Авито Работа"
    hint = "объявления Авито; только фрагменты, может блокироваться"
    enabled_by_default = False
    checked_by_default = False
    # Classified ads: anyone can post, so the text must never become the shared canonical copy.
    trusted = False

    def _fetcher(self) -> PageFetcher:
        # Search pages are cached for an hour: the same query is not re-requested within a run.
        return make_fetcher(self.cfg.options.get("fetcher", "http"), use_proxy=self.cfg.use_proxy,
                            min_interval=self.opt_float("min_interval", 5.0, floor=5.0), cache_ttl=3600)

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        location = str(self.cfg.options.get("location", "all"))
        if not _SLUG_RE.match(location):
            raise SourceError("Авито: неверный параметр location в конфиге")
        if not self.take_query_slot():
            return []
        include_similar = bool(self.cfg.options.get("include_similar", False))
        drafts: dict[str, VacancyDraft] = {}
        fetcher = self._fetcher()
        try:
            for page_no in range(1, self.opt_int("pages", 1, lo=1, hi=5) + 1):
                params: dict[str, Any] = {"q": query.text}
                if page_no > 1:
                    params["p"] = page_no
                page = await fetcher.get(f"{SITE}/{location}/vakansii", params)
                if page.looks_blocked or page.status == 439:
                    raise SourceError("Авито ограничил доступ (проверка безопасности)")
                if page.status >= 400:
                    raise SourceError(f"Авито ответил {page.status}")
                found = parse_search_page(page.text, include_similar=include_similar)
                for d in found:
                    drafts.setdefault(d.external_id, d)
                if not found or len(drafts) >= limit:
                    break
        finally:
            await fetcher.close()
        return list(drafts.values())[:limit]
