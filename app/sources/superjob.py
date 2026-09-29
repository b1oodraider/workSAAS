"""SuperJob (superjob.ru) — official API, needs a free application key.

Get the key ("Secret key") at https://api.superjob.ru after registering an app,
then set it in .env as SUPERJOB_API_KEY (or options.api_key_env to another name).

Options:
  api_key_env  env var with the key (default SUPERJOB_API_KEY)
  town         town id or name, e.g. 4 (Москва); empty = everywhere
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft
from app.sources.web import fetch_json

API = "https://api.superjob.ru/2.0/vacancies/"


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    if not item.get("id") or not item.get("profession"):
        return None
    town = item.get("town") or {}
    place = item.get("place_of_work") or {}
    experience = item.get("experience") or {}
    ts = item.get("date_published")
    currency = (item.get("currency") or "").upper()
    return VacancyDraft(
        source="superjob",
        external_id=str(item["id"]),
        title=str(item["profession"])[:300],
        url=item.get("link"),
        company=item.get("firm_name"),
        location=town.get("title") if isinstance(town, dict) else None,
        salary_from=item.get("payment_from") or None,
        salary_to=item.get("payment_to") or None,
        currency="RUR" if currency in ("RUB", "RUR") else (currency or None),
        remote=True if "удал" in str(place.get("title", "")).lower() else None,
        experience=experience.get("title") if isinstance(experience, dict) else None,
        description=html_to_text(item.get("vacancyRichText") or item.get("candidat") or ""),
        published_at=datetime.fromtimestamp(ts) if isinstance(ts, (int, float)) else None,
    )


class SuperJobSource(JobSource):
    name = "superjob"
    enabled_by_default = False
    title = "SuperJob"
    hint = "нужен бесплатный API-ключ"

    def _key(self) -> str:
        env = self.cfg.options.get("api_key_env") or "SUPERJOB_API_KEY"
        key = os.environ.get(env, "")
        if not key:
            raise SourceError(f"SuperJob: не задан API-ключ в переменной {env}")
        return key

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        params: dict[str, Any] = {"keyword": query.text, "count": min(limit, 100), "order_field": "date"}
        if self.cfg.options.get("town"):
            params["town"] = self.cfg.options["town"]
        if query.filters.salary_min:
            params["payment_from"] = query.filters.salary_min
        if query.filters.remote_only:
            params["place_of_work"] = 2  # 2 = remote in SuperJob's dictionary
        if query.filters.period_days:
            params["period"] = 7 if query.filters.period_days <= 7 else 0
        data = await fetch_json(API, params=params, use_proxy=self.cfg.use_proxy, source=self.title,
                                headers={"X-Api-App-Id": self._key()})
        objects = data.get("objects") if isinstance(data, dict) else None
        return [d for d in (item_to_draft(o) for o in objects or [] if isinstance(o, dict)) if d][:limit]
