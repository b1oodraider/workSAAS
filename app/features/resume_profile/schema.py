from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Skill(BaseModel):
    name: str = Field(description="Один навык/инструмент/технология без версий и скобок, как в вакансиях: 'PostgreSQL', '1С:Бухгалтерия'")
    level: Literal["core", "secondary"] = Field(
        description="core — основной, подтверждён опытом; secondary — упомянут/второстепенный"
    )


class ResumeProfile(BaseModel):
    headline: str = Field(description="Кто это одной строкой, напр. 'Middle Python backend-разработчик'")
    seniority: Literal["intern", "junior", "middle", "senior", "lead", "principal", "unknown"]
    years_experience: float | None = Field(None, description="Стаж по профессии в годах (только работа по профессии), если можно оценить")
    roles: list[str] = Field(
        description="3-8 названий должностей без грейда, как в заголовках вакансий, с синонимами и английским вариантом: "
                    "'Python-разработчик', 'Python Developer', 'Backend-разработчик', 'Программист'"
    )
    skills: list[Skill] = Field(description="core — до 12, secondary — до 15")
    domains: list[str] = Field(description="До 5 отраслей/доменов опыта: финтех, e-commerce, ...")
    work_format: Literal["remote", "hybrid", "office", "any", "unknown"]
    search_queries: list[str] = Field(
        description="4-8 коротких поисковых запросов для job-сайтов: название, синонимы, английский вариант, должность + навык, один широкий"
    )
    negative_keywords: list[str] = Field(
        default_factory=list,
        description="Однозначные слова-исключения из явных пожеланий кандидата (отрасль, продукт, технология)",
    )
