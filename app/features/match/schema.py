from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Gap(BaseModel):
    requirement: str = Field(description="Требование из вакансии, коротко")
    importance: Literal["must", "nice"]
    how_to_close: str = Field(description="Как закрыть/подать этот пробел: что подчеркнуть, чему научиться")


class MatchResult(BaseModel):
    # Reasoning fields first: models without hidden thinking commit to a number last.
    summary: str = Field(description="2-3 предложения: почему такая оценка")
    matched: list[str] = Field(description="До 5 требований вакансии, которые кандидат закрывает, с опорой на опыт")
    gaps: list[Gap] = Field(description="До 5")
    risks: list[str] = Field(description="До 4: что может смутить работодателя или кандидата (грейд, формат, домен, зарплата)")
    talking_points: list[str] = Field(description="До 5 фактов из резюме, которые стоит подчеркнуть в отклике и на интервью")
    score: int = Field(ge=0, le=100, description="Насколько кандидат подходит под вакансию, по шкале из инструкции")
    verdict: Literal["strong", "good", "stretch", "weak", "mismatch"]
    recommendation: Literal["apply", "apply_with_caveats", "skip"]
