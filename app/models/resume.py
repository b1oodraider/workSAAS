from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, utcnow


class Resume(TimestampMixin, Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Free-form wishes that are not in the resume: "only remote, no gambling", etc.
    preferences: Mapped[str] = mapped_column(Text, default="")
    # Structured search criteria (app.services.criteria.SearchCriteria): salary floor, countries…
    criteria: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    # Used to invalidate the derived search profile after edits.
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
