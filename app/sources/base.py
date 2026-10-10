"""JobSource port: every vacancy provider returns normalised VacancyDraft objects."""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from typing import Any, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.core.errors import UserError

from app.core.config import SourceConfig


log = logging.getLogger(__name__)
T = TypeVar("T")

# What a parser may raise on unexpected third-party data.
ITEM_ERRORS = (ValidationError, AttributeError, TypeError, KeyError, ValueError, OverflowError, IndexError)
# Salaries at or above this are parsing noise, not money (any currency, per month or year).
MAX_SALARY = 10**9


class SourceError(UserError):
    pass


def safe_map(fn: Callable[[Any], "VacancyDraft | None"], items: Iterable[Any], *,
             source: str = "") -> list["VacancyDraft"]:
    """Parse items one by one: a malformed item is logged and skipped, never breaks the batch."""
    result = []
    for item in items or []:
        try:
            draft = fn(item)
        except ITEM_ERRORS as exc:
            log.warning("%s: skipped malformed item: %s", source or "source", exc)
            continue
        if draft is not None:
            result.append(draft)
    return result


def as_str(value: Any) -> str | None:
    """Third-party JSON often has objects where strings are expected ({"name": ...})."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("name", "title", "value", "@value"):
            if isinstance(value.get(key), str):
                return value[key].strip() or None
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def naive_utc(dt: datetime) -> datetime:
    """Aware datetime -> naive UTC (the DB stores naive UTC); naive values are kept as is."""
    if not dt.tzinfo:
        return dt
    try:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (OverflowError, ValueError):  # "0001-01-01T00:00+03:00": keep the date, drop the offset
        return dt.replace(tzinfo=None)


class SearchFilters(BaseModel):
    """User-facing filters. Sources apply what they can natively; the prefilter applies the rest."""

    area: str = ""  # source-specific region, e.g. hh area id "1" (Москва), "113" (Россия)
    salary_min: int | None = None
    remote_only: bool = False
    experience: str = ""  # hh: noExperience | between1And3 | between3And6 | moreThan6
    period_days: int = 14
    exclude_words: list[str] = Field(default_factory=list)
    # Filled from "hide with a reason": checked against the title only / the employer name.
    exclude_title_words: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)


class SearchQuery(BaseModel):
    text: str
    filters: SearchFilters = Field(default_factory=SearchFilters)


class VacancyDraft(BaseModel):
    source: str
    external_id: str
    title: str
    url: str | None = None
    company: str | None = None
    location: str | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    currency: str | None = None
    salary_gross: bool | None = None
    remote: bool | None = None
    experience: str | None = None
    employment: str | None = None
    skills: list[str] = Field(default_factory=list)
    description: str = ""
    is_partial: bool = False
    published_at: datetime | None = None
    raw: dict[str, Any] | None = None

    @field_validator("salary_from", "salary_to", mode="before")
    @classmethod
    def _sane_salary(cls, value: Any) -> Any:
        """Untrusted text carries any number ("ЗП 99999999999999999999", "1e30"): a value outside
        0 < v < MAX_SALARY is no salary at all (and would overflow the SQLite INTEGER column)."""
        if value is None or isinstance(value, bool):
            return value
        try:
            number = float(value.strip() if isinstance(value, str) else value)
        except OverflowError:
            return None
        except (TypeError, ValueError):
            return value  # not a number: let the int validation reject it as before
        return value if math.isfinite(number) and 0 < number < MAX_SALARY else None

    def updated(self, **changes: Any) -> "VacancyDraft":
        """A copy with ``changes`` validated like the constructor's arguments (``model_copy``
        skips validation, so a parsed salary would bypass the range check)."""
        return type(self).model_validate({**self.model_dump(), **changes})


def query_words(query: str) -> list[str]:
    """Distinct lower-cased query words used for local matching (single letters dropped)."""
    return list(dict.fromkeys(w.lower() for w in query.split() if len(w) > 1))


def word_hits(text: str, words: list[str]) -> int:
    """How many of ``words`` (from ``query_words``) occur in ``text``."""
    low = text.lower()
    return sum(1 for w in words if w in low)


def matches_query(text: str, query: str, *, all_words: bool = False) -> bool:
    """Client-side filter for sources without server-side search (feeds, channels)."""
    words = query_words(query)
    if not words:
        return True
    hits = word_hits(text, words)
    return hits == len(words) if all_words else hits > 0


def relevance_key(title: str, body: str, published_at: datetime | None, words: list[str]) -> tuple:
    """Sort key (use with reverse=True) for local search: more query words in the title first,
    then more words in the body, then newest. "Java Developer" no longer ties with
    "Python Developer" for the query "python developer"."""
    return word_hits(title, words), word_hits(body, words), published_at or datetime.min


class JobSource(ABC):
    name: str = ""
    title: str = ""
    # False for sources that cannot search (manual input).
    searchable: bool = True
    # Pre-selected in the "new search" form.
    checked_by_default: bool = True
    # Short hint shown in the UI next to the source name.
    hint: str = ""
    # Used when config.toml has no [sources.<name>] section.
    enabled_by_default: bool = True
    # Trusted sources publish vacancies themselves (job boards). Untrusted ones carry text
    # written by arbitrary people (Telegram posts, RSS, manual input): their copy of a
    # vacancy must never become the shared canonical one for other users.
    trusted: bool = True

    def __init__(self, cfg: SourceConfig) -> None:
        self.cfg = cfg
        # The search pipeline builds one instance per run and calls search() once per query:
        # per-instance state (memoised boards/feeds, query budget) lives for exactly one run.
        self._memo: dict[Any, Any] = {}
        self._memo_errors: dict[Any, SourceError] = {}
        self._queries_done = 0

    # --- options: bad config values are logged and replaced by defaults, never raise ---------

    def _bad_option(self, key: str, value: Any, default: Any) -> None:
        log.warning("%s: invalid option %s=%r, using %r", self.name or type(self).__name__, key, value, default)

    def opt_int(self, key: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
        value = self.cfg.options.get(key, default)
        try:
            if isinstance(value, bool) or value is None:
                raise TypeError
            result = int(float(value)) if isinstance(value, (str, float)) else int(value)
        except (TypeError, ValueError, OverflowError):
            self._bad_option(key, value, default)
            result = default
        if lo is not None:
            result = max(lo, result)
        if hi is not None:
            result = min(hi, result)
        return result

    def opt_float(self, key: str, default: float, floor: float | None = None) -> float:
        value = self.cfg.options.get(key, default)
        try:
            if isinstance(value, bool) or value is None:
                raise TypeError
            result = float(value)
            if not math.isfinite(result):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            self._bad_option(key, value, default)
            result = float(default)
        return max(floor, result) if floor is not None else result

    def opt_list(self, key: str, default: Iterable[Any] = ()) -> list[str]:
        """List option; a "a, b" string is split on commas. A missing/None option gives
        ``default``; an explicitly empty one (``[]`` or "") gives an empty list."""
        value = self.cfg.options.get(key)
        if value is None:
            value = list(default)
        if isinstance(value, str):
            value = value.split(",")
        elif not isinstance(value, (list, tuple)):
            self._bad_option(key, value, list(default))
            value = list(default)
        return [s for s in (str(v).strip() for v in value if v is not None) if s]

    # --- per-run helpers --------------------------------------------------------------------

    def take_query_slot(self, default: int = 3) -> bool:
        """Query budget for slow/risky scrapers: True for the first ``max_queries`` searches of
        this instance (one search run), False afterwards."""
        budget = self.opt_int("max_queries", default, lo=1, hi=20)
        self._queries_done += 1
        if self._queries_done == budget + 1:
            log.info("%s: query budget (%d) reached, later queries of this run do no network", self.name, budget)
        return self._queries_done <= budget

    async def memoized(self, key: Any, load: Callable[[], Awaitable[T]]) -> T:
        """Load once per instance (= per search run): parsed boards/feeds/pages are reused by
        every query; a failure is remembered too, so a dead board is not retried 8 times."""
        if key in self._memo:
            return self._memo[key]
        if key in self._memo_errors:
            raise self._memo_errors[key]
        try:
            value = await load()
        except SourceError as exc:
            self._memo_errors[key] = exc
            raise
        self._memo[key] = value
        return value

    def parse_once(self, key: Any, parse: Callable[[Any], "VacancyDraft | None"],
                   item: Any) -> "VacancyDraft | None":
        """``safe_map`` for one item, memoised per instance: long HTML descriptions of a feed are
        converted once per run, not once per query."""
        key = ("item", key)
        if key not in self._memo:
            found = safe_map(parse, [item], source=self.title or self.name)
            self._memo[key] = found[0] if found else None
        return self._memo[key]

    def is_memoized(self, key: Any) -> bool:
        return key in self._memo or key in self._memo_errors

    @abstractmethod
    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]: ...

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        """Full vacancy card. Sources whose search results are complete may skip this."""
        return None

    def external_id_from_url(self, url: str) -> str | None:
        """If ``url`` belongs to this source, return its external id."""
        return None
