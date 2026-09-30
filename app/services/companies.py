"""Employer rating from this instance's own data (no third-party reviews are collected).

Signals: did the employer answer applications, how fast (tracker status history), was a
rejection explained (what the user marked), how clear its vacancy texts are (latest
vacancy_review per vacancy) and 👎 votes with the reason "company".

Privacy and abuse (a handful of friends share one instance):
- data about applications and votes is used only once ``MIN_USERS`` different people
  contributed, one contribution per person (their latest application), and is shown as
  coarse categories, never as counts or dates — otherwise the badge would tell friends who
  applied where and what came of it;
- only vacancies from trusted sources count: manual imports and Telegram channels carry
  text anyone can write under any company name.

The rating is a light argument: it is shown next to vacancies and nudges sorting by at most
±5 points; it never changes the AI match score or auto-apply decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from statistics import median

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models import Analysis, UserVacancy, UserVacancyStatus, Vacancy
from app.services.vacancies import normalize_name, trusted_sources

MIN_USERS = 3
# One review run must not label a company: at least this many of its vacancies reviewed.
MIN_REVIEWS = 2
# No answer this long after applying counts as silence.
SILENT_AFTER = timedelta(days=14)
MAX_SORT_ADJUSTMENT = 5.0
_ANSWERED = {UserVacancyStatus.interview, UserVacancyStatus.offer, UserVacancyStatus.rejected}
# Only components with data take part in the weighted average.
_WEIGHTS = {"answer_rate": 0.35, "speed": 0.2, "informative": 0.2, "text_quality": 0.15, "dislikes": 0.1}


@dataclass
class CompanyRating:
    score: int | None = None  # 0..100, internal: drives the sort nudge, not shown as a number
    badges: list[str] = field(default_factory=list)

    @property
    def sort_adjustment(self) -> float:
        if self.score is None:
            return 0.0
        return round((self.score - 50) / 50 * MAX_SORT_ADJUSTMENT, 1)


@dataclass
class _Outcome:
    answered: bool
    days: float | None = None
    informative: bool | None = None  # None: the user did not say how they were rejected


def key(company: str | None) -> str:
    return normalize_name(company or "")


def _answer_time(uv) -> datetime | None:
    answered = {s.value for s in _ANSWERED}
    for entry in uv.status_history or []:
        if entry.get("status") in answered:
            try:
                at = datetime.fromisoformat(entry["at"])
            except (KeyError, TypeError, ValueError):
                continue
            if uv.applied_at is None or at >= uv.applied_at:
                return at
    return None


def _outcome(uv, now: datetime, tracks_answers: bool) -> _Outcome | None:
    """What came of one application; None while it is too early to tell.

    Silence counts only for people who mark answers in the tracker at all: auto-apply sets
    "applied" by itself, and an untouched tracker says nothing about the employer."""
    if uv.status in _ANSWERED:
        at = _answer_time(uv)
        days = (at - uv.applied_at).total_seconds() / 86400 if at and uv.applied_at else None
        if uv.status != UserVacancyStatus.rejected:
            informative = True  # an invitation says more than any rejection
        else:
            informative = {"reasoned": True, "template": False}.get(uv.response_quality)
        return _Outcome(answered=True, days=days, informative=informative)
    if (tracks_answers and uv.status == UserVacancyStatus.applied and uv.applied_at
            and now - uv.applied_at > SILENT_AFTER):
        return _Outcome(answered=False)
    return None


def _rate(outcomes: list[_Outcome], dislike_users: int, review_scores: list[float]) -> CompanyRating | None:
    parts: dict[str, float] = {}
    badges: list[str] = []
    if len(outcomes) >= MIN_USERS:
        rate = sum(o.answered for o in outcomes) / len(outcomes)
        parts["answer_rate"] = 100 * rate
        badges.append("обычно отвечает на отклики" if rate >= 0.7 else
                      "часто не отвечает на отклики" if rate <= 0.3 else "отвечает примерно на половину откликов")
        days = [o.days for o in outcomes if o.days is not None]
        if days:
            d = median(days)
            parts["speed"] = 100.0 if d <= 3 else 75.0 if d <= 7 else 50.0 if d <= 14 else 25.0
            badges.append("отвечает в первые дни" if d <= 3 else "отвечает в течение недели" if d <= 7
                          else "отвечает медленно")
        marked = [o.informative for o in outcomes if o.informative is not None]
        if marked:
            share = sum(marked) / len(marked)
            parts["informative"] = 100 * share
            if share < 0.5:
                badges.append("отказы обычно без объяснений")
    if dislike_users >= MIN_USERS:
        parts["dislikes"] = max(0.0, 100.0 - 25.0 * dislike_users)
        badges.append("не нравится нескольким пользователям")
    if len(review_scores) >= MIN_REVIEWS:
        quality = sum(review_scores) / len(review_scores)
        parts["text_quality"] = quality
        if quality >= 70:
            badges.append("понятные описания вакансий")
        elif quality < 50:
            badges.append("размытые описания вакансий")
    if not parts:
        return None
    total = sum(_WEIGHTS[k] for k in parts)
    return CompanyRating(score=round(sum(_WEIGHTS[k] * v for k, v in parts.items()) / total), badges=badges)


def ratings(s: Session, companies: list[str | None]) -> dict[str, CompanyRating]:
    """Ratings keyed by ``key(company)``; companies without enough data are absent."""
    wanted = {key(c) for c in companies if c} - {""}
    if not wanted:
        return {}
    # Manual imports are private copies with whatever company name the user typed.
    trusted = [name for name in trusted_sources() if name != "manual"]
    now = utcnow()
    latest_app: dict[str, dict[int, Any]] = {}  # company -> user -> latest application
    dislikes: dict[str, set[int]] = {}
    tracks_answers = set(s.scalars(select(UserVacancy.user_id).where(UserVacancy.status.in_(_ANSWERED)).distinct()))

    # Plain columns: this runs on every page view and must not load vacancy texts.
    rows = s.execute(
        select(Vacancy.company, UserVacancy.user_id, UserVacancy.status, UserVacancy.applied_at,
               UserVacancy.status_history, UserVacancy.response_quality, UserVacancy.match_vote,
               UserVacancy.match_vote_reason)
        .select_from(UserVacancy).join(Vacancy, Vacancy.id == UserVacancy.vacancy_id)
        .where(
            Vacancy.company.is_not(None), Vacancy.source.in_(trusted),
            or_(UserVacancy.applied_at.is_not(None), UserVacancy.match_vote_reason == "company"),
        )
    )
    for uv in rows:
        k = key(uv.company)
        if k not in wanted:
            continue
        if uv.match_vote == -1 and uv.match_vote_reason == "company":
            dislikes.setdefault(k, set()).add(uv.user_id)
        if uv.applied_at is not None:
            per_user = latest_app.setdefault(k, {})
            prev = per_user.get(uv.user_id)
            if prev is None or uv.applied_at > prev.applied_at:
                per_user[uv.user_id] = uv

    # Latest review of each vacancy: re-running the review must not add weight.
    latest_review = (
        select(func.max(Analysis.id)).where(Analysis.kind == "vacancy_review", Analysis.vacancy_id.is_not(None))
        .group_by(Analysis.vacancy_id)
    )
    reviews: dict[str, list[float]] = {}
    for company, score in s.execute(
        select(Vacancy.company, Analysis.output["overall_score"].as_float())
        .join(Analysis, Analysis.vacancy_id == Vacancy.id)
        .where(Analysis.id.in_(latest_review), Vacancy.company.is_not(None), Vacancy.source.in_(trusted))
    ):
        k = key(company)
        if k in wanted and isinstance(score, (int, float)):
            reviews.setdefault(k, []).append(float(score))

    result = {}
    for k in wanted:
        outcomes = [o for uv in latest_app.get(k, {}).values()
                    if (o := _outcome(uv, now, uv.user_id in tracks_answers)) is not None]
        rating = _rate(outcomes, len(dislikes.get(k, ())), reviews.get(k, []))
        if rating is not None:
            result[k] = rating
    return result
