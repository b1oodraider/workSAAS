from __future__ import annotations

from pydantic import BaseModel, Field


class ResumeChange(BaseModel):
    section: str = Field(description="Раздел резюме")
    before: str = Field(description="Точная цитата из резюме, которую менять; пусто, если это добавление")
    after: str = Field(description="Новый текст — только на фактах из резюме, переформулированных под вакансию")
    reason: str = Field(description="Какое требование вакансии это закрывает")


class TailoredResume(BaseModel):
    summary: str = Field(description="1-2 предложения: главная идея адаптации")
    new_headline: str = Field(description="Желаемая должность/заголовок резюме под эту вакансию")
    changes: list[ResumeChange] = Field(description="До 8 правок, самые важные сначала")
    keywords_to_add: list[str] = Field(description="До 10 ключевых слов из вакансии, которые кандидат РЕАЛЬНО может указать по опыту из резюме")
    do_not_claim: list[str] = Field(description="До 5 требований вакансии, которые по резюме не подтверждаются — их нельзя вписывать")
