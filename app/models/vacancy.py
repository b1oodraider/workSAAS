from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin, utcnow


class Vacancy(TimestampMixin, Base):
    """Normalised vacancy, shared between all users."""

    __tablename__ = "vacancies"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_vacancy_source_ext"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str | None] = mapped_column(String(300), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    salary_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    salary_gross: Mapped[bool | None] = mapped_column(nullable=True)
    remote: Mapped[bool | None] = mapped_column(nullable=True)
    experience: Mapped[str | None] = mapped_column(String(100), nullable=True)
    employment: Mapped[str | None] = mapped_column(String(100), nullable=True)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    # True when description is only a short snippet and full card wasn't fetched yet.
    is_partial: Mapped[bool] = mapped_column(default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Normalised "company|title" to merge the same vacancy found in several sources.
    dedup_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class UserVacancyStatus(str, enum.Enum):
    new = "new"
    saved = "saved"
    applied = "applied"
    interview = "interview"
    offer = "offer"
    rejected = "rejected"
    hidden = "hidden"


# Russian labels shared by the web UI and the bot.
STATUS_LABELS = {
    "new": "новая",
    "saved": "в избранном",
    "applied": "откликнулся",
    "interview": "собеседование",
    "offer": "оффер",
    "rejected": "отказ",
    "hidden": "скрыта",
}


class UserVacancy(TimestampMixin, Base):
    """Per-user view of a vacancy: pipeline status and prefilter score."""

    __tablename__ = "user_vacancies"
    __table_args__ = (UniqueConstraint("user_id", "vacancy_id", name="uq_user_vacancy"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"), index=True)
    status: Mapped[UserVacancyStatus] = mapped_column(
        Enum(UserVacancyStatus, native_enum=False, length=16), default=UserVacancyStatus.new
    )
    prefilter_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    search_id: Mapped[int | None] = mapped_column(
        ForeignKey("saved_searches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    notes: Mapped[str] = mapped_column(Text, default="")
    # When the user was notified (Telegram digest) about this vacancy.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Application tracking.
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    next_action_note: Mapped[str] = mapped_column(String(300), default="")
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # [{"status": "applied", "at": "2026-09-29T10:00:00"}, ...]
    status_history: Mapped[list[dict]] = mapped_column(JSON, default=list)

    vacancy: Mapped[Vacancy] = relationship(lazy="joined")
