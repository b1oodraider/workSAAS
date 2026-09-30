"""Cheap, LLM-free relevance score (0..100) used to pick which vacancies go to the LLM.

Implements the Ranker interface; an embeddings-based ranker could replace it
without touching the search pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from app.sources.base import SearchFilters

_TOKEN_RE = re.compile(r"[a-zа-яё0-9][a-zа-яё0-9+#.\-]*", re.I)


def normalize(text: str) -> str:
    return " " + " ".join(_TOKEN_RE.findall(text.lower().replace("ё", "е"))) + " "


@dataclass
class RankProfile:
    """What the ranker needs from a resume profile."""

    core_skills: list[str] = field(default_factory=list)
    secondary_skills: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    years_experience: float | None = None


@dataclass
class RankInput:
    title: str
    text: str
    salary_from: int | None = None
    salary_to: int | None = None
    remote: bool | None = None
    currency: str | None = None
    published_at: datetime | None = None
    experience: str | None = None  # as the source shows it: "3–6 лет", "более 6 лет", "не требуется"


@dataclass
class RankResult:
    score: float
    reasons: list[str]
    excluded: bool = False


class Ranker(Protocol):
    def score(self, profile: RankProfile, vacancy: RankInput, filters: SearchFilters) -> RankResult: ...


def _contains(haystack: str, phrase: str) -> bool:
    """Whole-token phrase match: 'Java' must not match 'JavaScript'."""
    needle = normalize(phrase).strip()
    return bool(needle) and f" {needle} " in haystack


_CYRILLIC_WORD_RE = re.compile(r"^[а-я]{5,}$")


def _contains_stem(haystack: str, phrase: str) -> bool:
    """Stop words: Russian words of 5+ letters also match inflected forms ('гемблинг' ->
    'гемблинга'); anything else is a whole-token match, so 'Java' does not exclude
    'JavaScript' and 'go' does not exclude 'Google'."""
    needle = normalize(phrase).strip()
    if not needle:
        return False
    if _CYRILLIC_WORD_RE.match(needle.split()[-1]):
        return f" {needle}" in haystack
    return f" {needle} " in haystack


# Currencies the salary filter understands as rubles (the filter value is in rubles).
RUBLE_CODES = {None, "", "RUR", "RUB"}

# Older postings get fewer answers: (max age in days, score multiplier). Unknown date -> 1.0.
FRESHNESS = [(3, 1.0), (7, 0.95), (14, 0.9), (30, 0.8)]
STALE_MULTIPLIER = 0.7


def age_days(published_at: datetime | None, now: datetime | None = None) -> float | None:
    if published_at is None:
        return None
    if published_at.tzinfo is not None:
        published_at = published_at.astimezone(UTC).replace(tzinfo=None)
    now = now or datetime.now(UTC).replace(tzinfo=None)
    return max((now - published_at).total_seconds() / 86400, 0.0)


_YEARS_RE = re.compile(r"(\d+)")
# A vacancy that wants this many times more experience than the candidate has is "another grade".
GRADE_GAP = 2.0
GRADE_MULTIPLIER = 0.6


def min_years(experience: str | None) -> int | None:
    """'1–3 года' -> 1, 'более 6 лет' -> 6, 'не требуется' -> 0; unknown -> None."""
    if not experience:
        return None
    text = experience.lower()
    if "не треб" in text or "без опыта" in text:
        return 0
    m = _YEARS_RE.search(text)
    return int(m.group(1)) if m else None


def freshness(age: float | None) -> float:
    if age is None:
        return 1.0
    return next((m for days, m in FRESHNESS if age <= days), STALE_MULTIPLIER)


class KeywordRanker:
    def score(self, profile: RankProfile, vacancy: RankInput, filters: SearchFilters) -> RankResult:
        title = normalize(vacancy.title)
        body = normalize(vacancy.title + " " + vacancy.text)

        for word in [*filters.exclude_words, *profile.negative_keywords]:
            if _contains_stem(body, word):
                return RankResult(0.0, [f"стоп-слово «{word}»"], excluded=True)
        if filters.remote_only and vacancy.remote is False:
            return RankResult(0.0, ["не удалёнка"], excluded=True)
        # Salary filter is in rubles: never compare against dollars/euros/tenge.
        if (filters.salary_min and vacancy.salary_to and vacancy.currency in RUBLE_CODES
                and vacancy.salary_to < filters.salary_min):
            return RankResult(0.0, [f"зарплата до {vacancy.salary_to} < {filters.salary_min}"], excluded=True)
        # Not every source filters by date natively; the period applies to all of them here.
        age = age_days(vacancy.published_at)
        if age is not None and filters.period_days and age > filters.period_days:
            return RankResult(0.0, [f"опубликована больше {filters.period_days} дн. назад"], excluded=True)

        weights = [(s, 2.0) for s in profile.core_skills] + [(s, 1.0) for s in profile.secondary_skills]
        total = sum(w for _, w in weights) or 1.0
        matched = [s for s, _ in weights if _contains(body, s)]
        skill_part = sum(w for s, w in weights if s in matched) / total

        role_hit = next((r for r in profile.roles if _contains(title, r)), None)
        # Partial title match: any significant word of a role in the title.
        if role_hit is None:
            for role in profile.roles:
                words = [w for w in normalize(role).split() if len(w) > 3]
                if words and any(f" {w} " in title for w in words):
                    role_hit = role
                    break
        title_part = 1.0 if role_hit else 0.0

        fresh = freshness(age)
        wanted = min_years(vacancy.experience)
        grade = 1.0
        # "1–3 года" is the usual entry band on hh; only 3+ years can be "another grade".
        if (wanted and wanted >= 3 and profile.years_experience is not None
                and wanted >= GRADE_GAP * max(profile.years_experience, 0.5)):
            grade = GRADE_MULTIPLIER
        score = round((70 * skill_part + 30 * title_part) * fresh * grade, 1)
        reasons = []
        if fresh < 1.0:
            reasons.append(f"опубликована {int(age)} дн. назад")
        if grade < 1.0:
            reasons.append(f"нужен опыт от {wanted} лет")
        if matched:
            reasons.append("навыки: " + ", ".join(matched[:8]))
        if role_hit:
            reasons.append(f"роль: {role_hit}")
        return RankResult(score, reasons)
