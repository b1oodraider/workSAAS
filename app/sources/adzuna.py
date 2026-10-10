"""Adzuna (adzuna.com) — job search aggregator for the UK, EU and other countries; official API.

Needs a free key: register at https://developer.adzuna.com/ and set ADZUNA_APP_ID and
ADZUNA_APP_KEY in .env (or name other variables in options.app_id_env / options.app_key_env).

API: GET https://api.adzuna.com/v1/api/jobs/{country}/search/1?app_id=...&app_key=...
     &what=...&results_per_page=N&content-type=application/json
Search results carry only a description snippet (``is_partial``); salaries are in the
country's currency and are usually annual. Adzuna's terms require linking to ``redirect_url``.

NOT VERIFIED LIVE: written from the public docs without a key; the test fixture is synthetic.

Options:
  countries    country codes to search (default ["gb", "de", "nl", "pl"])
  app_id_env   env var with the application id (default ADZUNA_APP_ID)
  app_key_env  env var with the application key (default ADZUNA_APP_KEY)
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft, as_dict, as_str, safe_map
from app.sources.jsonld import parse_date
from app.sources.web import fetch_json

log = logging.getLogger(__name__)

API = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
PAGE_MAX = 50
DEFAULT_COUNTRIES = ["gb", "de", "nl", "pl"]
CURRENCY = {
    "gb": "GBP", "us": "USD", "ca": "CAD", "au": "AUD", "nz": "NZD", "in": "INR", "sg": "SGD",
    "za": "ZAR", "br": "BRL", "mx": "MXN", "pl": "PLN", "ch": "CHF",
    "de": "EUR", "nl": "EUR", "fr": "EUR", "at": "EUR", "be": "EUR", "it": "EUR", "es": "EUR",
}
_COUNTRY_RE = re.compile(r"^[a-z]{2}$")
_URL_RE = re.compile(r"adzuna\.[a-z.]+/(?:[a-z-]+/)?(?:jobs/details|jobs/land/ad|details)/(\d+)")
_CONTRACT = {"full_time": "полная занятость", "part_time": "частичная занятость",
             "permanent": "постоянная работа", "contract": "контракт"}


def _num(value: Any) -> int | None:
    try:
        return int(float(value)) or None
    except (TypeError, ValueError):
        return None


def item_to_draft(item: dict[str, Any], country: str = "") -> VacancyDraft | None:
    if not item.get("id") or not item.get("title"):
        return None
    title = html_to_text(str(item["title"]))  # search terms come wrapped in <strong>
    desc = html_to_text(item.get("description") or "")
    sal_from, sal_to = _num(item.get("salary_min")), _num(item.get("salary_max"))
    currency = CURRENCY.get(country)
    if (sal_from or sal_to) and str(item.get("salary_is_predicted")) == "1":
        desc = f"Зарплата оценена Adzuna, работодатель её не указал\n\n{desc}"
    if (sal_from or sal_to) and not currency:
        # Unknown country currency: numbers would mislead the ranking, keep them as text only.
        desc = f"Зарплата: {sal_from or ''}–{sal_to or ''} (валюта страны {country or '?'})\n\n{desc}"
        sal_from = sal_to = None
    employment = ", ".join(_CONTRACT[k] for k in (item.get("contract_time"), item.get("contract_type"))
                           if k in _CONTRACT)
    category = as_str(as_dict(item.get("category")).get("label"))
    return VacancyDraft(
        source="adzuna",
        external_id=str(item["id"]),
        title=title[:300],
        url=item.get("redirect_url"),
        company=as_str(as_dict(item.get("company")).get("display_name")),
        location=as_str(as_dict(item.get("location")).get("display_name")),
        salary_from=sal_from,
        salary_to=sal_to,
        currency=currency if (sal_from or sal_to) else None,
        remote=True if "remote" in f"{title} {desc}".lower() else None,
        employment=employment or None,
        skills=[category] if category else [],
        description=desc,
        is_partial=True,
        published_at=parse_date(item.get("created")),
    )


class AdzunaSource(JobSource):
    name = "adzuna"
    title = "Adzuna"
    hint = "нужен бесплатный API-ключ"
    checked_by_default = False
    enabled_by_default = False
    # An aggregator: it re-publishes ads scraped from other boards and agencies, so its copy
    # must never become the shared canonical vacancy.
    trusted = False

    def _keys(self) -> tuple[str, str]:
        id_env = self.cfg.options.get("app_id_env") or "ADZUNA_APP_ID"
        key_env = self.cfg.options.get("app_key_env") or "ADZUNA_APP_KEY"
        app_id, app_key = os.environ.get(id_env, ""), os.environ.get(key_env, "")
        if not app_id or not app_key:
            raise SourceError(f"Adzuna: не задан API-ключ в переменных {id_env} и {key_env}")
        return app_id, app_key

    def _countries(self) -> list[str]:
        countries = self.opt_list("countries") or DEFAULT_COUNTRIES
        return [c for c in (x.lower() for x in countries) if _COUNTRY_RE.match(c)][:10]

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        app_id, app_key = self._keys()
        params: dict[str, Any] = {"app_id": app_id, "app_key": app_key, "what": query.text,
                                  "results_per_page": min(limit, PAGE_MAX),
                                  "content-type": "application/json", "sort_by": "date"}
        if query.filters.period_days:
            params["max_days_old"] = query.filters.period_days
        drafts: list[VacancyDraft] = []
        error: SourceError | None = None
        for country in self._countries():
            try:
                data = await fetch_json(API.format(country=country), params=params,
                                        use_proxy=self.cfg.use_proxy, source=self.title,
                                        min_interval=1.0, cache_ttl=3600)
            except SourceError as exc:
                log.warning("%s: country %s failed: %s", self.title, country, exc)
                error = exc
                continue
            results = data.get("results") if isinstance(data, dict) else None
            drafts += safe_map(lambda item, c=country: item_to_draft(item, c), results or [],
                               source=self.title)
        if not drafts and error is not None:
            raise error
        drafts.sort(key=lambda d: d.published_at or datetime.min, reverse=True)
        return drafts[:limit]

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
