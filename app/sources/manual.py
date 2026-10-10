"""Manual input: user pastes vacancy text. Not searchable; exists so it can be enabled/disabled."""

from __future__ import annotations

from app.sources.base import JobSource, SearchQuery, VacancyDraft


class ManualSource(JobSource):
    name = "manual"
    title = "Вручную"
    searchable = False
    # Pasted text and arbitrary imported pages: never the shared canonical copy.
    trusted = False

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        return []
