"""Candidate search criteria ("Условия поиска"): what a resume usually doesn't say but the
search needs — the real salary floor, countries, formats, relocation, languages, grade flexibility.

Stored per resume (`resumes.criteria`, JSON). Used three ways:
- hard filters in the prefilter (salary floor, remote only, avoided industries) — `effective_filters`;
- the `<preferences>` block of LLM prompts (match, quick match, profile) — `preferences_text`;
- a "what's still missing" list shown to the user — `missing`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from app.services import resumes as resume_svc
from app.services.errors import ValidationFailed
from app.sources.base import SearchFilters

Grade = Literal["intern", "junior", "middle", "senior", "lead"]
WorkFormat = Literal["remote", "hybrid", "office"]
Employment = Literal["full", "part", "project", "internship"]
Contract = Literal["employment", "sole_trader", "self_employed", "b2b"]
Relocation = Literal["", "no", "city", "country", "abroad"]
Currency = Literal["RUB", "USD", "EUR", "KZT", "GBP"]

LABELS = {
    "grades": {"intern": "стажёр", "junior": "junior", "middle": "middle", "senior": "senior", "lead": "lead"},
    "work_formats": {"remote": "удалёнка", "hybrid": "гибрид", "office": "офис"},
    "employment": {"full": "полная занятость", "part": "частичная", "project": "проектная работа",
                   "internship": "стажировка"},
    "contracts": {"employment": "трудовой договор", "sole_trader": "ИП", "self_employed": "самозанятость",
                  "b2b": "B2B-контракт с иностранной компанией"},
    "relocation": {"": "не указано", "no": "без переезда", "city": "готов переехать в другой город",
                   "country": "готов переехать внутри страны", "abroad": "готов к релокации за рубеж"},
}
MAX_ITEMS = 20


def _clean_list(value: object) -> list[str]:
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    if not isinstance(value, list):
        return []
    seen: dict[str, str] = {}
    for item in value:
        text = " ".join(str(item).split())[:80]
        if text and text.lower() not in seen:
            seen[text.lower()] = text
    return list(seen.values())[:MAX_ITEMS]


class SearchCriteria(BaseModel):
    """Everything is optional: an empty field means "no restriction / not asked yet"."""

    salary_min: int | None = Field(None, ge=0, le=100_000_000)      # the real floor, per month
    salary_desired: int | None = Field(None, ge=0, le=100_000_000)
    salary_currency: Currency = "RUB"
    salary_net: bool = True                                           # "на руки"
    work_formats: list[WorkFormat] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)               # in priority order
    cities: list[str] = Field(default_factory=list)
    relocation: Relocation = ""
    timezone: str = Field("", max_length=40)                          # e.g. "UTC+3, ±3 часа"
    citizenship: str = Field("", max_length=120)
    work_permits: list[str] = Field(default_factory=list)            # e.g. "ЕС (Blue Card)"
    languages: list[str] = Field(default_factory=list)               # e.g. "английский B2"
    employment: list[Employment] = Field(default_factory=list)
    contracts: list[Contract] = Field(default_factory=list)
    grades: list[Grade] = Field(default_factory=list)                # grades the candidate targets
    # Apply where the vacancy asks for more experience/grade if the skills fit well.
    stretch_up: bool = True
    adjacent_roles: bool = True                                       # consider related roles too
    roles_extra: list[str] = Field(default_factory=list)             # roles the user adds by hand
    industries_prefer: list[str] = Field(default_factory=list)
    industries_avoid: list[str] = Field(default_factory=list)
    start: str = Field("", max_length=120)                            # "через 2 недели", "сразу"
    must_have: str = Field("", max_length=1000)
    deal_breakers: str = Field("", max_length=1000)

    _lists = field_validator("countries", "cities", "work_permits", "languages", "roles_extra",
                             "industries_prefer", "industries_avoid", mode="before")(_clean_list)

    @field_validator("timezone", "citizenship", "start", mode="before")
    @classmethod
    def _one_line(cls, value: object) -> str:
        return " ".join(str(value or "").split())

    @field_validator("must_have", "deal_breakers", mode="before")
    @classmethod
    def _text(cls, value: object) -> str:
        return str(value or "").strip()


def load(raw: object) -> SearchCriteria:
    """Stored JSON -> criteria; unknown or broken values never break a search."""
    try:
        return SearchCriteria.model_validate(raw or {})
    except ValidationError:
        return SearchCriteria()


FIELD_TITLES = {
    "salary_min": "Минимальная зарплата", "salary_desired": "Желаемая зарплата", "salary_currency": "Валюта",
    "work_formats": "Формат работы", "countries": "Страны", "cities": "Города", "relocation": "Переезд",
    "timezone": "Часовой пояс", "citizenship": "Гражданство", "work_permits": "Разрешения на работу",
    "languages": "Языки", "employment": "Занятость", "contracts": "Оформление", "grades": "Уровень",
    "roles_extra": "Ещё роли", "industries_prefer": "Интересные отрасли", "industries_avoid": "Отрасли — нет",
    "start": "Когда готов выйти", "must_have": "Обязательно", "deal_breakers": "Точно нет",
}


def get(s: Session, user_id: int, resume_id: int) -> SearchCriteria:
    return load(resume_svc.get_owned(s, user_id, resume_id).criteria)


def save(s: Session, user_id: int, resume_id: int, data: dict) -> SearchCriteria:
    resume = resume_svc.get_owned(s, user_id, resume_id)
    try:
        criteria = SearchCriteria.model_validate(data)
    except ValidationError as exc:
        err = exc.errors()[0]
        field = str(err["loc"][0]) if err.get("loc") else ""
        raise ValidationFailed(f"Поле «{FIELD_TITLES.get(field, field)}»: проверьте значение") from exc
    if criteria.salary_min and criteria.salary_desired and criteria.salary_desired < criteria.salary_min:
        raise ValidationFailed("Желаемая зарплата меньше минимальной")
    resume.criteria = criteria.model_dump(mode="json")
    return criteria


# What to ask when it's empty, most important first ("уточните, чтобы поиск был точнее").
QUESTIONS = (
    ("salary_min", "Минимальная зарплата, которую вы реально готовы рассмотреть (а не желаемая)"),
    ("work_formats", "Формат работы: удалёнка, гибрид, офис"),
    ("countries", "Страны, где ищете работу или работодателя (по приоритету)"),
    ("relocation", "Готовы ли к переезду и куда"),
    ("languages", "Языки и уровень (например, английский B2)"),
    ("grades", "На какой уровень претендуете (junior / middle / senior / lead)"),
    ("contracts", "Как можете оформиться: трудовой договор, ИП, самозанятость, B2B"),
    ("citizenship", "Гражданство и разрешения на работу (важно для зарубежных вакансий)"),
    ("industries_avoid", "Отрасли или компании, куда точно не хотите"),
    ("start", "Когда готовы выйти на новую работу"),
)


def missing(c: SearchCriteria) -> list[str]:
    return [q for field, q in QUESTIONS if not getattr(c, field)]


def _money(value: int, currency: str) -> str:
    return f"{value:,}".replace(",", " ") + f" {currency}"


def render(c: SearchCriteria, *, employer_facing: bool = False) -> str:
    """Criteria as plain Russian lines for the `<preferences>` block of prompts ("" if empty).
    For texts that reach an employer the private parts (salary floor, citizenship, deal breakers)
    are left out: a letter must never reveal the minimum the candidate would accept."""
    lines = []
    net = "на руки" if c.salary_net else "до вычета налогов"
    if c.salary_min and not employer_facing:
        lines.append(f"Минимальная зарплата (ниже не рассматривает): {_money(c.salary_min, c.salary_currency)} "
                     f"в месяц, {net}.")
    if c.salary_desired:
        lines.append(f"Желаемая зарплата: {_money(c.salary_desired, c.salary_currency)} в месяц, {net}.")
    for field in ("work_formats", "employment", "contracts", "grades"):
        values = getattr(c, field)
        if values:
            lines.append(f"{FIELD_TITLES[field]}: " + ", ".join(LABELS[field].get(v, v) for v in values) + ".")
    if c.countries:
        lines.append("Страны (по приоритету): " + ", ".join(c.countries) + ".")
    if c.cities:
        lines.append("Города: " + ", ".join(c.cities) + ".")
    if c.relocation:
        lines.append("Переезд: " + LABELS["relocation"][c.relocation] + ".")
    private = ("citizenship", "work_permits") if employer_facing else ()
    for field in ("timezone", "citizenship", "start"):
        if getattr(c, field) and field not in private:
            lines.append(f"{FIELD_TITLES[field]}: {getattr(c, field)}.")
    for field in ("work_permits", "languages", "roles_extra", "industries_prefer", "industries_avoid"):
        if getattr(c, field) and field not in private:
            lines.append(f"{FIELD_TITLES[field]}: " + ", ".join(getattr(c, field)) + ".")
    # Facts only — the scoring rules live in the system prompts (they treat grade/years as soft and
    # look at adjacent roles by default); a refusal must be said here explicitly.
    if not c.stretch_up:
        lines.append("Не хочет вакансий, где требуют больше опыта или грейд выше, чем у него.")
    if not c.adjacent_roles:
        lines.append("Смежные роли не рассматривает — только свою профессию.")
    if c.must_have:
        lines.append(f"Обязательно: {c.must_have}")
    if c.deal_breakers and not employer_facing:
        lines.append(f"Точно нет: {c.deal_breakers}")
    return "\n".join(lines)


def preferences_text(resume, *, employer_facing: bool = False) -> str:
    """Structured criteria + the free-form wishes: what prompts see inside <preferences>."""
    parts = [render(load(getattr(resume, "criteria", None)), employer_facing=employer_facing),
             (resume.preferences or "").strip()]
    return "\n".join(p for p in parts if p)


def limits(c: SearchCriteria) -> list[str]:
    """Hard limits the criteria add to every search, as short labels for the search page."""
    f = effective_filters(SearchFilters(), c)
    out = []
    if f.salary_min:
        out.append("зарплата от " + _money(f.salary_min, "₽"))
    if f.remote_only:
        out.append("только удалёнка")
    out += [f"в тексте: {w}" for w in f.exclude_words]
    return out


def effective_filters(filters: SearchFilters, c: SearchCriteria) -> SearchFilters:
    """Search filters tightened by the candidate's hard limits (never loosened)."""
    f = filters.model_copy(deep=True)
    if c.salary_min and c.salary_currency == "RUB":
        f.salary_min = max(f.salary_min or 0, c.salary_min)
    if c.work_formats == ["remote"]:
        f.remote_only = True
    words = {w.lower() for w in f.exclude_words}
    f.exclude_words += [w for w in c.industries_avoid if w.lower() not in words][:MAX_ITEMS]
    return f
