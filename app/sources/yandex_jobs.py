"""Яндекс (yandex.ru/jobs) — vacancies of Yandex itself, via the JSON API of the careers site.

Search:  GET https://yandex.ru/jobs/api/publications?text=<q>[&work_modes=remote][&cursor=...]
         -> {"count", "next", "results": [{id, publication_slug_url, title, short_summary,
             vacancy: {cities[], skills[], work_modes[]}, public_service, modified}]}
         (~20 results per page whatever ``page_size`` says; ``next`` carries a ``cursor``.)
Card:    GET https://yandex.ru/jobs/api/publications/<id> — the same plus the full text
         (description, duties, key_qualifications, additional_requirements, conditions, ...).
Page:    https://yandex.ru/jobs/vacancies/<publication_slug_url> (the slug ends with -<id>).

Not a documented public API (it is the site's own frontend backend), may change.
JSON shape verified live on 2026-10-08.

Options:
  pages         max result pages per query (1..5, default 2)
  min_interval  seconds between requests (default 1.0, at least 1.0)
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, VacancyDraft, as_dict, as_str, safe_map
from app.sources.jsonld import parse_date
from app.sources.web import fetch_json

API = "https://yandex.ru/jobs/api/publications"
SITE = "https://yandex.ru/jobs/vacancies/"
COMPANY = "Яндекс"
_URL_RE = re.compile(r"yandex\.(?:ru|com)/jobs/vacancies/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)")
_PRO_LEVEL_RE = re.compile(r"pro_level\.(\w+)")

# Full-card text fields in display order.
_SECTIONS = (
    ("description", ""),
    ("duties", "Обязанности"),
    ("key_qualifications", "Ключевые требования"),
    ("additional_requirements", "Будет плюсом"),
    ("tech_stack", "Стек технологий"),
    ("conditions", "Условия"),
    ("our_team", "О команде"),
)


def _names(items: Any) -> list[str]:
    return [n for n in (as_str(i) for i in items or [] if isinstance(i, dict)) if n]


def _slugs(items: Any) -> list[str]:
    return [str(i.get("slug")) for i in items or [] if isinstance(i, dict) and i.get("slug")]


def _url(item: dict[str, Any]) -> str:
    slug = item.get("publication_slug_url")
    return SITE + (str(slug) if slug else str(item["id"]))


def _common(item: dict[str, Any]) -> dict[str, Any]:
    vacancy = as_dict(item.get("vacancy"))
    modes = _slugs(vacancy.get("work_modes"))
    level = _PRO_LEVEL_RE.search(str(vacancy.get("pro_level_min_display") or ""))
    return {
        "source": "yandex_jobs",
        "external_id": str(item["id"]),
        "title": str(item["title"]).strip()[:300],
        "url": _url(item),
        "company": COMPANY,
        "location": ", ".join(_names(vacancy.get("cities"))) or None,
        "remote": True if "remote" in modes else None,
        "experience": level.group(1) if level else None,
        "skills": _names(vacancy.get("skills"))[:30],
        "published_at": parse_date(item.get("published_at") or item.get("modified")),
    }


def _header(item: dict[str, Any]) -> list[str]:
    vacancy = as_dict(item.get("vacancy"))
    lines = []
    service = as_str(as_dict(item.get("public_service")).get("name"))
    if service:
        lines.append(f"Сервис: {service}")
    modes = _names(vacancy.get("work_modes"))
    if modes:
        lines.append("Формат работы: " + ", ".join(modes))
    return lines


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    """Search result: short summary only."""
    if not item.get("id") or not item.get("title"):
        return None
    lines = _header(item)
    summary = html_to_text(item.get("short_summary") or "")
    if summary:
        lines += ["", summary]
    return VacancyDraft(**_common(item), description="\n".join(lines).strip(), is_partial=True)


def card_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    """Full publication from /api/publications/<id>."""
    if not item.get("id") or not item.get("title"):
        return None
    parts = ["\n".join(_header(item))]
    for key, heading in _SECTIONS:
        text = html_to_text(item.get(key) if isinstance(item.get(key), str) else "")
        if text:
            parts.append(f"{heading}:\n{text}" if heading else text)
    body = "\n\n".join(p for p in parts if p).strip()
    has_text = any(isinstance(item.get(k), str) and item[k].strip() for k, _ in _SECTIONS)
    if not has_text:
        summary = html_to_text(item.get("short_summary") or "")
        body = f"{body}\n\n{summary}".strip()
    return VacancyDraft(**_common(item), description=body, is_partial=not has_text)


def next_cursor(data: Any) -> str | None:
    nxt = data.get("next") if isinstance(data, dict) else None
    if not isinstance(nxt, str) or not nxt:
        return None
    values = parse_qs(urlsplit(nxt).query).get("cursor")
    return values[0] if values else None


class YandexJobsSource(JobSource):
    name = "yandex_jobs"
    title = "Яндекс"
    hint = "вакансии самого Яндекса"
    checked_by_default = False

    def _interval(self) -> float:
        return self.opt_float("min_interval", 1.0, floor=1.0)

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        drafts: dict[str, VacancyDraft] = {}
        cursor: str | None = None
        for _ in range(self.opt_int("pages", 2, lo=1, hi=5)):
            params: dict[str, Any] = {"text": query.text, "page_size": 20}
            if query.filters.remote_only:
                params["work_modes"] = "remote"
            if cursor:
                params["cursor"] = cursor
            data = await fetch_json(API, params=params, use_proxy=self.cfg.use_proxy, source=self.title,
                                    min_interval=self._interval())
            items = data.get("results") if isinstance(data, dict) else None
            if not items:
                break
            for d in safe_map(item_to_draft, items, source=self.title):
                drafts.setdefault(d.external_id, d)
            cursor = next_cursor(data)
            if len(drafts) >= limit or not cursor:
                break
        return list(drafts.values())[:limit]

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        if not external_id.isdigit():
            return None
        data = await fetch_json(f"{API}/{external_id}", use_proxy=self.cfg.use_proxy, source=self.title,
                                min_interval=self._interval())
        found = safe_map(card_to_draft, [data] if isinstance(data, dict) else [], source=self.title)
        return found[0] if found else None

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
