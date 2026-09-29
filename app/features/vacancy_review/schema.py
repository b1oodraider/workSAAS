from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Flag(BaseModel):
    severity: Literal["high", "medium", "low"]
    text: str
    evidence: str = Field(description="Цитата или факт из вакансии, на котором основан вывод")


class VacancyReview(BaseModel):
    overall_score: int = Field(ge=0, le=100, description="Насколько вакансия качественная и привлекательная для кандидата")
    summary: str = Field(description="2-3 предложения: что это за работа на самом деле")
    seniority_guess: str = Field(description="Реальный уровень, который ищут, судя по требованиям")
    must_have: list[str] = Field(description="Действительно обязательные требования")
    nice_to_have: list[str]
    red_flags: list[Flag]
    green_flags: list[str]
    salary_assessment: str = Field(description="Оценка зарплаты/её отсутствия относительно требований, без выдуманных цифр рынка")
    clarity_score: int = Field(ge=0, le=10, description="Насколько понятно описаны задачи, команда, условия")
    hidden_expectations: list[str] = Field(description="Что подразумевается между строк")
    questions_to_ask: list[str] = Field(description="Вопросы работодателю до/на собеседовании")
