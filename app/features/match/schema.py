from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Gap(BaseModel):
    requirement: str
    importance: Literal["must", "nice"]
    how_to_close: str = Field(description="Как закрыть/подать этот пробел: что подчеркнуть, чему научиться")


class MatchResult(BaseModel):
    score: int = Field(ge=0, le=100, description="Насколько кандидат подходит под вакансию")
    verdict: Literal["strong", "good", "stretch", "weak", "mismatch"]
    recommendation: Literal["apply", "apply_with_caveats", "skip"]
    summary: str = Field(description="2-3 предложения: почему такая оценка")
    matched: list[str] = Field(description="Требования вакансии, которые кандидат закрывает, с опорой на опыт")
    gaps: list[Gap]
    risks: list[str] = Field(description="Что может смутить работодателя или кандидата (грейд, формат, домен, зарплата)")
    talking_points: list[str] = Field(description="Факты из резюме, которые стоит подчеркнуть в отклике и на интервью")
