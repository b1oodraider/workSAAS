from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Flag(BaseModel):
    severity: Literal["high", "medium", "low"]
    text: str = Field(description="Суть флага, коротко")
    evidence: str = Field(description="Цитата или факт из вакансии, на котором основан вывод")


class VacancyReview(BaseModel):
    summary: str = Field(description="2-3 предложения: что это за работа на самом деле")
    seniority_guess: str = Field(description="Реальный уровень, который ищут, судя по требованиям")
    must_have: list[str] = Field(description="До 7 действительно обязательных требований")
    nice_to_have: list[str] = Field(description="До 5")
    red_flags: list[Flag] = Field(description="До 6")
    green_flags: list[str] = Field(description="До 5")
    salary_assessment: str = Field(description="Оценка зарплаты/её отсутствия относительно требований, без выдуманных цифр рынка")
    hidden_expectations: list[str] = Field(
        description="До 4 неявных ожиданий, каждое — с опорой на формулировку из вакансии; пусто, если опоры нет"
    )
    questions_to_ask: list[str] = Field(description="До 5 вопросов работодателю до/на собеседовании")
    clarity_score: int = Field(
        ge=0, le=10,
        description="10 — задачи, стек, команда, условия и вилка указаны; 0 — только общие слова",
    )
    overall_score: int = Field(
        ge=0, le=100,
        description="Привлекательность для кандидата: 80+ понятные задачи, вилка, адекватные требования; "
        "50-79 есть пробелы в описании; меньше 50 — серьёзные красные флаги",
    )
