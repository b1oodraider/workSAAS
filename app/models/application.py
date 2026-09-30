from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin
from app.models.vacancy import Vacancy


class ApplicationStatus(str, enum.Enum):
    queued = "queued"        # selected, waiting for its turn (or for approval in confirm mode)
    approved = "approved"    # confirm mode: the user said yes
    sending = "sending"      # a job is applying right now
    applied = "applied"
    skipped = "skipped"      # the applier decided not to (questionnaire, already applied, closed)
    failed = "failed"
    cancelled = "cancelled"  # the user said no


ACTIVE_APPLICATION_STATUSES = (ApplicationStatus.queued, ApplicationStatus.approved, ApplicationStatus.sending)


class Application(TimestampMixin, Base):
    """One (auto) application to a vacancy: selection -> letter -> sending."""

    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("user_id", "vacancy_id", name="uq_application_user_vacancy"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"), index=True)
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[ApplicationStatus] = mapped_column(
        Enum(ApplicationStatus, native_enum=False, length=16), default=ApplicationStatus.queued, index=True
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    letter: Mapped[str] = mapped_column(Text, default="")
    # Why it was skipped/failed, or what the applier reported.
    reason: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # When the user was told about the result / asked to confirm (Telegram).
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    vacancy: Mapped[Vacancy] = relationship(lazy="joined")


class AutoApplySettings(Base):
    """Per-user auto-apply configuration. Hard limits are enforced in the service, not here."""

    __tablename__ = "autoapply_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # "auto": send without asking; "confirm": ask in Telegram / on the web page first.
    mode: Mapped[str] = mapped_column(String(16), default="confirm")
    min_score: Mapped[int] = mapped_column(Integer, default=80)
    daily_limit: Mapped[int] = mapped_column(Integer, default=10)
    min_interval_s: Mapped[int] = mapped_column(Integer, default=120)
    # Local hours when applications may be sent (like a human would).
    active_from_hour: Mapped[int] = mapped_column(Integer, default=9)
    active_to_hour: Mapped[int] = mapped_column(Integer, default=21)
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True)
    # Which resume to choose on hh.ru when the account has several (substring of its title).
    hh_resume_title: Mapped[str] = mapped_column(String(200), default="")
    letter_tone: Mapped[str] = mapped_column(String(16), default="friendly")
    # Set by the kill switch (captcha, login expired, repeated failures); cleared by the user.
    paused_reason: Mapped[str] = mapped_column(Text, default="")
    paused_notified: Mapped[bool] = mapped_column(Boolean, default=False)
