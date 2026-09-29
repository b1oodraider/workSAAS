"""JobSource port: every vacancy provider returns normalised VacancyDraft objects."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import SourceConfig


class SourceError(Exception):
    pass


class SearchFilters(BaseModel):
    """User-facing filters. Sources apply what they can natively; the prefilter applies the rest."""

    area: str = ""  # source-specific region, e.g. hh area id "1" (Москва), "113" (Россия)
    salary_min: int | None = None
    remote_only: bool = False
    experience: str = ""  # hh: noExperience | between1And3 | between3And6 | moreThan6
    period_days: int = 14
    exclude_words: list[str] = Field(default_factory=list)


class SearchQuery(BaseModel):
    text: str
    filters: SearchFilters = Field(default_factory=SearchFilters)


class VacancyDraft(BaseModel):
    source: str
    external_id: str
    title: str
    url: str | None = None
    company: str | None = None
    location: str | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    currency: str | None = None
    salary_gross: bool | None = None
    remote: bool | None = None
    experience: str | None = None
    employment: str | None = None
    skills: list[str] = Field(default_factory=list)
    description: str = ""
    is_partial: bool = False
    published_at: datetime | None = None
    raw: dict[str, Any] | None = None


class JobSource(ABC):
    name: str = ""
    title: str = ""
    # False for sources that cannot search (manual input).
    searchable: bool = True

    def __init__(self, cfg: SourceConfig) -> None:
        self.cfg = cfg

    @abstractmethod
    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]: ...

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        """Full vacancy card. Sources whose search results are complete may skip this."""
        return None

    def external_id_from_url(self, url: str) -> str | None:
        """If ``url`` belongs to this source, return its external id."""
        return None
