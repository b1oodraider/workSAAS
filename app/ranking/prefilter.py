"""Cheap, LLM-free relevance score (0..100) used to pick which vacancies go to the LLM.

Implements the Ranker interface; an embeddings-based ranker could replace it
without touching the search pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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


@dataclass
class RankInput:
    title: str
    text: str
    salary_from: int | None = None
    salary_to: int | None = None
    remote: bool | None = None


@dataclass
class RankResult:
    score: float
    reasons: list[str]
    excluded: bool = False


class Ranker(Protocol):
    def score(self, profile: RankProfile, vacancy: RankInput, filters: SearchFilters) -> RankResult: ...


def _contains(haystack: str, phrase: str) -> bool:
    needle = normalize(phrase).strip()
    return bool(needle) and f" {needle} " in haystack


class KeywordRanker:
    def score(self, profile: RankProfile, vacancy: RankInput, filters: SearchFilters) -> RankResult:
        title = normalize(vacancy.title)
        body = normalize(vacancy.title + " " + vacancy.text)

        for word in [*filters.exclude_words, *profile.negative_keywords]:
            if _contains(body, word):
                return RankResult(0.0, [f"стоп-слово «{word}»"], excluded=True)
        if filters.remote_only and vacancy.remote is False:
            return RankResult(0.0, ["не удалёнка"], excluded=True)
        if filters.salary_min and vacancy.salary_to and vacancy.salary_to < filters.salary_min:
            return RankResult(0.0, [f"зарплата до {vacancy.salary_to} < {filters.salary_min}"], excluded=True)

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

        score = round(70 * skill_part + 30 * title_part, 1)
        reasons = []
        if matched:
            reasons.append("навыки: " + ", ".join(matched[:8]))
        if role_hit:
            reasons.append(f"роль: {role_hit}")
        return RankResult(score, reasons)
