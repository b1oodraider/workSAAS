from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Skill(BaseModel):
    name: str = Field(description="Одна технология/навык без версий и скобок, как в вакансиях: 'PostgreSQL', 'FastAPI'")
    level: Literal["core", "secondary"] = Field(
        description="core — основной, подтверждён опытом; secondary — упомянут/второстепенный"
    )


class ResumeProfile(BaseModel):
    headline: str = Field(description="Кто это одной строкой, напр. 'Middle Python backend-разработчик'")
    seniority: Literal["intern", "junior", "middle", "senior", "lead", "principal", "unknown"]
    years_experience: float | None = Field(description="Суммарный релевантный опыт в годах, если можно оценить")
    roles: list[str] = Field(
        description="2-5 названий должностей без грейда, как в заголовках вакансий: 'Python-разработчик', 'Backend Developer'"
    )
    skills: list[Skill] = Field(description="core — до 12, secondary — до 15")
    domains: list[str] = Field(description="До 5 отраслей/доменов опыта: финтех, e-commerce, ...")
    work_format: Literal["remote", "hybrid", "office", "any", "unknown"]
    search_queries: list[str] = Field(
        description="3-6 коротких поисковых запросов для job-сайтов (как их ввёл бы человек в поиск hh.ru)"
    )
    negative_keywords: list[str] = Field(
        default_factory=list,
        description="Однозначные слова-исключения из явных пожеланий кандидата (отрасль, продукт, технология)",
    )
