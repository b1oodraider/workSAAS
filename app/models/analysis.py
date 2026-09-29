from __future__ import annotations

from sqlalchemy import JSON, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class Analysis(TimestampMixin, Base):
    """Result of any LLM feature. ``kind`` = feature name, ``output`` = its schema dump.

    One generic table keeps new features migration-free. Values that must be
    sortable/filterable in SQL (like ``score``) get their own column.
    """

    __tablename__ = "analyses"
    __table_args__ = (Index("ix_analyses_kind_subject", "kind", "resume_id", "vacancy_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), nullable=True
    )
    vacancy_id: Mapped[int | None] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=True
    )
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON)
    score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
