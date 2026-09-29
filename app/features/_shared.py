"""Helpers shared by feature context builders. Features read each other's results
from the analyses table through here instead of importing each other."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Analysis


def latest_output(s: Session, user_id: int, kind: str, *, resume_id: int | None = None,
                  vacancy_id: int | None = None, vacancy_level: bool = False) -> dict[str, Any] | None:
    q = select(Analysis).where(Analysis.user_id == user_id, Analysis.kind == kind)
    if vacancy_level:
        q = q.where(Analysis.resume_id.is_(None))
    elif resume_id is not None:
        q = q.where(Analysis.resume_id == resume_id)
    if vacancy_id is not None:
        q = q.where(Analysis.vacancy_id == vacancy_id)
    a = s.scalar(q.order_by(Analysis.id.desc()).limit(1))
    if a is None:
        return None
    # Outputs made by an older prompt version may miss fields the templates expect.
    from app.features import FEATURES

    feature = FEATURES.get(kind)
    if feature is not None:
        try:
            return feature.task.output.model_validate(a.output).model_dump(mode="json")
        except ValidationError:
            return None
    return dict(a.output)


def pair_context(s: Session, user_id: int, resume, vacancy, *kinds: str) -> dict[str, Any]:
    """Latest outputs of the given kinds for this resume/vacancy pair (vacancy-level kinds too)."""
    ctx: dict[str, Any] = {}
    for kind in kinds:
        out = latest_output(s, user_id, kind, resume_id=resume.id, vacancy_id=vacancy.id)
        if out is None:
            # Vacancy-level analyses (vacancy_review) have no resume. Never fall back to an
            # analysis made for another resume: its "facts" would leak into this one.
            out = latest_output(s, user_id, kind, vacancy_id=vacancy.id, vacancy_level=True)
        ctx[kind] = out
    return ctx
