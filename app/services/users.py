"""Per-user preferences, shared by the web settings page and the bot."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import User
from app.services import resumes as resume_svc

NOTIFY_OFF = 101  # above any score


def notify_threshold(user: User) -> int:
    return user.notify_min_score if user.notify_min_score is not None else get_settings().telegram.notify_min_score


def set_notify_threshold(s: Session, user_id: int, value: int | None) -> None:
    """None -> default from settings; NOTIFY_OFF -> no notifications."""
    s.get(User, user_id).notify_min_score = None if value is None else max(0, min(value, NOTIFY_OFF))


def set_default_resume(s: Session, user_id: int, resume_id: int | None) -> str | None:
    user = s.get(User, user_id)
    if resume_id is None:
        user.default_resume_id = None
        return None
    resume = resume_svc.get_owned(s, user_id, resume_id)
    user.default_resume_id = resume.id
    return resume.title
