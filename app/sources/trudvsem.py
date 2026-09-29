"""«Работа России» (trudvsem.ru) — open government API, no key required.

API: http://opendata.trudvsem.ru/api/v1/vacancies?text=...&offset=N&limit=M
Many vacancies from state and large employers; IT coverage is modest.

Options:
  base_url  default "http://opendata.trudvsem.ru/api/v1"
  region    region code, e.g. "7700000000000" (Москва); empty = all Russia
"""

from __future__ import annotations

import re
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, VacancyDraft, as_dict, as_str, safe_map
from app.sources.jsonld import parse_date
from app.sources.web import fetch_json

DEFAULT_BASE = "http://opendata.trudvsem.ru/api/v1"
PAGE_SIZE = 100
MAX_PAGES = 10


def _int(v: Any) -> int | None:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n or None


def item_to_draft(vac: dict[str, Any]) -> VacancyDraft | None:
    vid = vac.get("id")
    title = vac.get("job-name") or vac.get("job_name")
    if not vid or not title:
        return None
    company = as_dict(vac.get("company"))
    region = as_dict(vac.get("region"))
    req = as_dict(vac.get("requirement"))
    parts = [html_to_text(vac.get("duty") or "")]
    if isinstance(req, dict):
        if req.get("qualification"):
            parts.append("Требования: " + html_to_text(str(req["qualification"])))
        if req.get("education"):
            parts.append(f"Образование: {req['education']}")
    schedule = str(vac.get("schedule") or "")
    exp = req.get("experience") if isinstance(req, dict) else None
    return VacancyDraft(
        source="trudvsem",
        external_id=str(vid),
        title=str(title)[:300],
        url=vac.get("vac_url"),
        company=as_str(company.get("name")),
        location=as_str(region.get("name")),
        salary_from=_int(vac.get("salary_min")),
        salary_to=_int(vac.get("salary_max")),
        currency="RUR",
        remote=True if re.search(r"удал[её]н|дистанц", schedule, re.I) else None,
        experience=f"от {exp} лет" if exp not in (None, "", 0, "0") else None,
        employment=as_str(vac.get("employment")),
        description="\n".join(p for p in parts if p),
        published_at=parse_date(vac.get("creation-date")),
    )


class TrudvsemSource(JobSource):
    name = "trudvsem"
    title = "Работа России"
    hint = "госпортал trudvsem.ru, без ключа"
    checked_by_default = False

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        base = str(self.cfg.options.get("base_url") or DEFAULT_BASE).rstrip("/")
        region = self.cfg.options.get("region") or ""
        url = f"{base}/vacancies/region/{region}" if region else f"{base}/vacancies"
        drafts: list[VacancyDraft] = []
        # The API's "offset" is a page number, not an item offset (per its documentation).
        offset = 0
        while len(drafts) < limit and offset < MAX_PAGES:
            data = await fetch_json(url, params={"text": query.text, "offset": offset,
                                                 "limit": min(PAGE_SIZE, limit)},
                                    use_proxy=self.cfg.use_proxy, source=self.title)
            results = (data or {}).get("results") or {}
            items = (results.get("vacancies") or []) if isinstance(results, dict) else []
            page = safe_map(lambda w: item_to_draft(w["vacancy"]), items, source=self.title)
            drafts += page
            if len(items) < min(PAGE_SIZE, limit) or not page:
                break
            offset += 1
        return drafts[:limit]
