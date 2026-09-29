from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Skill(BaseModel):
    name: str = Field(description="Название навыка/технологии в том виде, как его пишут в вакансиях")
    level: Literal["core", "secondary"] = Field(
        description="core — основной, подтверждён опытом; secondary — упомянут/второстепенный"
    )


class ResumeProfile(BaseModel):
    headline: str = Field(description="Кто это одной строкой, напр. 'Middle Python backend-разработчик'")
    seniority: Literal["intern", "junior", "middle", "senior", "lead", "principal", "unknown"]
    years_experience: float | None = Field(description="Суммарный релевантный опыт в годах, если можно оценить")
    roles: list[str] = Field(description="2-5 названий должностей, на которые кандидату стоит откликаться")
    skills: list[Skill]
    domains: list[str] = Field(description="Отрасли/домены опыта: финтех, e-commerce, ...")
    languages: list[str] = Field(description="Иностранные языки с уровнем")
    locations: list[str] = Field(description="Город(а) проживания/желаемой работы, если указаны")
    work_format: Literal["remote", "hybrid", "office", "any", "unknown"]
    salary_expectation: str | None = Field(description="Ожидания по зарплате, если указаны")
    search_queries: list[str] = Field(
        description="3-6 коротких поисковых запросов для job-сайтов (как их ввёл бы человек в поиск hh.ru)"
    )
    negative_keywords: list[str] = Field(
        description="Слова-исключения из пожеланий кандидата (чего он не хочет), напр. '1С', 'гемблинг'"
    )
