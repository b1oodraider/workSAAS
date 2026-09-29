from __future__ import annotations

from pydantic import BaseModel, Field


class OfferReview(BaseModel):
    assessment: str = Field(description="Разбор оффера относительно вакансии и пожеланий кандидата, без выдуманных рыночных цифр")
    leverage: list[str] = Field(description="До 5 аргументов кандидата для торга — из резюме и вакансии")
    risks: list[str] = Field(description="До 4 рисков/неясностей в условиях")
    questions_to_ask: list[str] = Field(description="До 5 вопросов по офферу (налоги, бонусы, испытательный срок, пересмотр)")
    red_lines: list[str] = Field(
        description="До 4 условий из самого оффера или вакансии, при которых стоит насторожиться; пусто, если таких нет")
    counter_script: str = Field(description="Готовый текст ответа на оффер с контрпредложением (если кандидат указал желаемое) или с уточнениями")
