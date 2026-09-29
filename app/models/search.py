from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class SavedSearch(TimestampMixin, Base):
    """A resume + sources + filters, optionally re-run periodically."""

    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    sources: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Extra queries on top of the ones generated from the resume profile.
    queries: Mapped[list[str]] = mapped_column(JSON, default=list)
    # SearchFilters.model_dump(), see app.sources.base
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    # 0 -> manual only
    interval_minutes: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
