"""Queries over match results shared by the web UI, the bot and digests."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Analysis, UserVacancy, UserVacancyStatus, Vacancy

OPEN_STATUSES = (UserVacancyStatus.new.value, UserVacancyStatus.saved.value)


def top_matches(s: Session, user_id: int, *, limit: int | None = None, min_score: float | None = None,
                unnotified: bool = False) -> list[tuple[UserVacancy, Vacancy, Analysis]]:
    """Open (new/saved) vacancies with their latest match analysis, best first."""
    latest = (
        select(Analysis.vacancy_id, func.max(Analysis.id).label("aid"))
        .where(Analysis.user_id == user_id, Analysis.kind == "match")
        .group_by(Analysis.vacancy_id).subquery()
    )
    q = (
        select(UserVacancy, Vacancy, Analysis)
        .join(Vacancy, Vacancy.id == UserVacancy.vacancy_id)
        .join(latest, latest.c.vacancy_id == Vacancy.id)
        .join(Analysis, Analysis.id == latest.c.aid)
        .where(UserVacancy.user_id == user_id, UserVacancy.status.in_(OPEN_STATUSES))
        .order_by(Analysis.score.desc().nullslast(), Analysis.id.desc())
    )
    if min_score is not None:
        q = q.where(Analysis.score >= min_score)
    if unnotified:
        q = q.where(UserVacancy.notified_at.is_(None))
    if limit:
        q = q.limit(limit)
    return [tuple(row) for row in s.execute(q).all()]
