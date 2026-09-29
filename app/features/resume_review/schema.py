from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ResumeIssue(BaseModel):
    severity: Literal["critical", "major", "minor"]
    section: str = Field(description="Раздел резюме, к которому относится проблема")
    problem: str
    fix: str = Field(description="Конкретно что сделать")
    example: str | None = Field(description="Пример переписанного фрагмента, если уместно")


class SectionScore(BaseModel):
    section: str
    score: int = Field(ge=0, le=10)
    comment: str


class ResumeReview(BaseModel):
    overall_score: int = Field(ge=0, le=100, description="Общая оценка резюме для целевой роли")
    verdict: str = Field(description="2-3 предложения: главное впечатление рекрутера")
    target_role: str = Field(description="Под какую роль оценивалось резюме")
    strengths: list[str]
    issues: list[ResumeIssue] = Field(description="Отсортированы от критичных к мелким")
    section_scores: list[SectionScore]
    ats_notes: list[str] = Field(description="Проблемы для автоматических фильтров (ATS) и поиска на job-сайтах")
    missing_keywords: list[str] = Field(description="Важные для целевой роли ключевые слова, которых нет в резюме")
    improved_summary: str = Field(description="Переписанный блок «О себе»/summary, 3-5 предложений, без выдуманных фактов")
