from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ResumeIssue(BaseModel):
    severity: Literal["critical", "major", "minor"]
    section: str = Field(description="Раздел резюме, к которому относится проблема")
    problem: str = Field(description="Что не так, коротко и конкретно")
    fix: str = Field(description="Конкретно что сделать")
    example: str | None = Field(None, description="Пример переписанного фрагмента, если уместно")


class SectionScore(BaseModel):
    section: str
    score: int = Field(ge=0, le=10)
    comment: str


class ResumeReview(BaseModel):
    target_role: str = Field(description="Под какую роль оценивалось резюме")
    verdict: str = Field(description="2-3 предложения: главное впечатление рекрутера")
    strengths: list[str] = Field(description="До 5 пунктов")
    issues: list[ResumeIssue] = Field(description="До 8, отсортированы от критичных к мелким")
    section_scores: list[SectionScore]
    ats_notes: list[str] = Field(description="До 5 проблем для автоматических фильтров (ATS) и поиска на job-сайтах")
    missing_keywords: list[str] = Field(description="До 10 важных для целевой роли ключевых слов, которых нет в резюме")
    improved_summary: str = Field(description="Переписанный блок «О себе»/summary, 3-5 предложений, без выдуманных фактов")
    overall_score: int = Field(ge=0, le=100, description="Итоговая оценка резюме для целевой роли по шкале из инструкции")
