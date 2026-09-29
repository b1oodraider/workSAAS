from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LetterIssue(BaseModel):
    severity: Literal["critical", "major", "minor"]
    problem: str = Field(description="Что не так, с цитатой из письма")
    fix: str = Field(description="Как исправить")


class LetterCritique(BaseModel):
    verdict: str = Field(description="2-3 предложения: прочитает ли рекрутер до конца и почему")
    strengths: list[str] = Field(description="До 3")
    issues: list[LetterIssue] = Field(description="До 6, от критичных к мелким")
    unsupported_claims: list[str] = Field(description="До 5 цитат из письма, которых нет в резюме; пусто, если таких нет")
    rewritten: str = Field(description="Исправленная версия письма не длиннее исходной, только на фактах из резюме")
    score: int = Field(ge=0, le=100, description="85+ отправлять как есть; 60-84 поправить; меньше 60 переписать")
