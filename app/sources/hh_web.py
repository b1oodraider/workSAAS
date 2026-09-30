"""Parsers for hh.ru web pages (used when the API is unavailable).

hh.ru markup changes from time to time, so every parser tries several strategies
from most to least structured and never raises on unexpected shapes:

search page:  1) JSON state embedded in the page  2) ``data-qa`` markup  3) bare vacancy links
vacancy page: 1) schema.org JobPosting JSON-LD      2) ``data-qa`` markup
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.core.text import html_to_text
from app.sources.base import VacancyDraft, as_dict, as_str, safe_map
from app.sources.jsonld import find_job_posting, walk
from app.sources.salary import _DIGITS_RE, parse_salary_text  # noqa: F401  (re-exported)

VACANCY_URL = "https://hh.ru/vacancy/{id}"
_ID_RE = re.compile(r"/vacancy/(\d+)")


def _text(el: Tag | None) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _qa(root: Tag, *names: str) -> Tag | None:
    for name in names:
        el = root.find(attrs={"data-qa": name})
        if el is not None:
            return el
    return None


def _naive_utc(dt: datetime) -> datetime:
    """The DB stores naive UTC: convert, don't just drop the offset (Moscow time is +3h)."""
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
        try:
            return _naive_utc(datetime.strptime(value.replace("Z", "+0000"), fmt))
        except ValueError:
            continue
    try:
        return _naive_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


# --------------------------------------------------------------------------- search page


def _embedded_state(soup: BeautifulSoup) -> Any | None:
    for tag in soup.find_all(["template", "script"]):
        tag_id = (tag.get("id") or "").lower()
        if "initialstate" in tag_id or "initial-state" in tag_id:
            try:
                return json.loads(tag.string or tag.get_text())
            except (json.JSONDecodeError, TypeError):
                continue
    return None


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


# hh experience codes (search page state) -> the labels vacancy pages show.
_EXPERIENCE = {"noExperience": "не требуется", "between1And3": "1–3 года", "between3And6": "3–6 лет",
               "moreThan6": "более 6 лет"}


def _draft_from_state_item(item: dict[str, Any]) -> VacancyDraft | None:
    vid = _first(item, "vacancyId", "id")
    name = _first(item, "name", "title")
    if not vid or not isinstance(name, str):
        return None
    company = item.get("company") or item.get("employer") or {}
    area = item.get("area") or {}
    comp = as_dict(item.get("compensation") or item.get("salary"))
    snippet = as_dict(item.get("snippet"))
    parts = [html_to_text(str(snippet.get(k) or "")) for k in ("req", "requirement", "resp", "responsibility")]
    work = json.dumps([item.get(k) for k in ("workSchedule", "@workSchedule", "schedule", "workFormats")])
    links = as_dict(item.get("links"))
    pub = item.get("publicationTime") or item.get("publishedAt") or item.get("creationTime")
    if isinstance(pub, dict):
        pub = pub.get("$") or pub.get("value")
    return VacancyDraft(
        source="hh",
        external_id=str(vid),
        title=name,
        url=links.get("desktop") or VACANCY_URL.format(id=vid),
        company=as_str(_first(company, "visibleName", "name") if isinstance(company, dict) else company),
        location=as_str(area),
        salary_from=comp.get("from") if isinstance(comp, dict) else None,
        salary_to=comp.get("to") if isinstance(comp, dict) else None,
        currency=(_first(comp, "currencyCode", "currency") if isinstance(comp, dict) else None),
        salary_gross=comp.get("gross") if isinstance(comp, dict) else None,
        remote=True if "remote" in work.lower() else None,
        description="\n".join(p for p in parts if p),
        is_partial=True,
        published_at=_parse_dt(pub),
        experience=_EXPERIENCE.get(str(item.get("workExperience") or "")),
    )


def _drafts_from_state(state: Any) -> list[VacancyDraft]:
    drafts: dict[str, VacancyDraft] = {}
    candidates = [d for d in walk(state) if "vacancyId" in d and ("name" in d or "title" in d)]
    for draft in safe_map(_draft_from_state_item, candidates, source="hh web"):
        drafts.setdefault(draft.external_id, draft)
    return list(drafts.values())


def _drafts_from_markup(soup: BeautifulSoup) -> list[VacancyDraft]:
    drafts: dict[str, VacancyDraft] = {}
    for title_el in soup.find_all(attrs={"data-qa": re.compile(r"^serp-item__title")}):
        link = title_el if title_el.name == "a" else title_el.find_parent("a") or title_el.find("a")
        href = link.get("href", "") if link else ""
        m = _ID_RE.search(href)
        if not m or m.group(1) in drafts:
            continue
        card = title_el.find_parent(attrs={"data-qa": re.compile(r"vacancy-serp__vacancy")}) or \
            title_el.find_parent("div")
        card = card or soup
        salary = _text(_qa(card, "vacancy-serp__vacancy-compensation")) or ""
        if not salary:
            for span in card.find_all("span"):
                t = _text(span)
                if any(sym in t for sym in ("₽", "$", "€")) and _DIGITS_RE.search(t):
                    salary = t
                    break
        sal_from, sal_to, currency, gross = parse_salary_text(salary)
        desc = [
            _text(_qa(card, "vacancy-serp__vacancy_snippet_requirement")),
            _text(_qa(card, "vacancy-serp__vacancy_snippet_responsibility")),
        ]
        work = _text(_qa(card, "vacancy-label-remote-work-schedule", "vacancy-serp__vacancy-work-schedule"))
        vid = m.group(1)
        drafts[vid] = VacancyDraft(
            source="hh",
            external_id=vid,
            title=_text(title_el) or f"Вакансия {vid}",
            url=VACANCY_URL.format(id=vid),
            company=_text(_qa(card, "vacancy-serp__vacancy-employer-text",
                              "vacancy-serp__vacancy-employer")) or None,
            location=_text(_qa(card, "vacancy-serp__vacancy-address")) or None,
            salary_from=sal_from, salary_to=sal_to, currency=currency, salary_gross=gross,
            remote=True if "удал" in work.lower() else None,
            description="\n".join(d for d in desc if d),
            is_partial=True,
        )
    return list(drafts.values())


def _drafts_from_links(soup: BeautifulSoup) -> list[VacancyDraft]:
    drafts: dict[str, VacancyDraft] = {}
    for a in soup.find_all("a", href=_ID_RE):
        vid = _ID_RE.search(a["href"]).group(1)
        title = _text(a)
        if vid in drafts or len(title) < 3:
            continue
        drafts[vid] = VacancyDraft(source="hh", external_id=vid, title=title,
                                   url=VACANCY_URL.format(id=vid), is_partial=True)
    return list(drafts.values())


def parse_search_page(html: str) -> list[VacancyDraft]:
    soup = BeautifulSoup(html, "html.parser")
    state = _embedded_state(soup)
    if state is not None:
        drafts = _drafts_from_state(state)
        if drafts:
            return drafts
    return _drafts_from_markup(soup) or _drafts_from_links(soup)


def has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return _qa(soup, "pager-next") is not None


# --------------------------------------------------------------------------- vacancy page


def parse_vacancy_page(html: str, external_id: str) -> VacancyDraft | None:
    soup = BeautifulSoup(html, "html.parser")
    jp = find_job_posting(soup) or {}

    title = jp.get("title") or _text(_qa(soup, "vacancy-title"))
    if not title:
        return None
    description = html_to_text(jp.get("description") or "")
    if not description:
        desc_el = _qa(soup, "vacancy-description")
        description = html_to_text(str(desc_el)) if desc_el else ""

    org = jp.get("hiringOrganization") or {}
    company = (org.get("name") if isinstance(org, dict) else None) or \
        _text(_qa(soup, "vacancy-company-name")) or None

    location = None
    loc = jp.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        addr = loc.get("address") or {}
        if isinstance(addr, dict):
            location = addr.get("addressLocality") or addr.get("addressRegion")
    location = location or _text(_qa(soup, "vacancy-view-location", "vacancy-view-raw-address")) or None

    sal_from = sal_to = None
    currency = None
    gross = None
    base = jp.get("baseSalary")
    if isinstance(base, dict):
        currency = base.get("currency")
        value = base.get("value") or {}
        if isinstance(value, dict):
            sal_from = value.get("minValue") or (value.get("value") if not value.get("maxValue") else None)
            sal_to = value.get("maxValue")
    salary_text = _text(_qa(soup, "vacancy-salary"))
    if salary_text:
        t_from, t_to, t_cur, gross = parse_salary_text(salary_text)
        sal_from, sal_to, currency = sal_from or t_from, sal_to or t_to, currency or t_cur

    skills = [_text(el) for el in soup.find_all(attrs={"data-qa": re.compile(r"^skills-element")})]
    if not skills:
        skills = [_text(el) for el in soup.find_all(attrs={"data-qa": "bloko-tag__text"})]
    remote = None
    if jp.get("jobLocationType") == "TELECOMMUTE":
        remote = True
    elif "удалён" in _text(_qa(soup, "work-formats-text", "vacancy-view-employment-mode")).lower():
        remote = True

    def _int(v: Any) -> int | None:
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    return VacancyDraft(
        source="hh",
        external_id=str(external_id),
        title=title,
        url=VACANCY_URL.format(id=external_id),
        company=company,
        location=location,
        salary_from=_int(sal_from),
        salary_to=_int(sal_to),
        currency="RUR" if currency in ("RUB", "RUR") else currency,
        salary_gross=gross,
        remote=remote,
        experience=_text(_qa(soup, "vacancy-experience")) or None,
        employment=_text(_qa(soup, "common-employment-text", "vacancy-view-employment-mode")) or None,
        skills=[s for s in dict.fromkeys(skills) if s],
        description=description,
        is_partial=not description,
        published_at=_parse_dt(jp.get("datePosted")),
    )
