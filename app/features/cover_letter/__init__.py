from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from app.features import register
from app.features._shared import latest_output
from app.features.base import AnalysisFeature, feature_dir
from app.features.cover_letter.schema import CoverLetter
from app.llm.tasks import LLMTask


class CoverLetterParams(BaseModel):
    tone: Literal["friendly", "formal", "concise", "enthusiastic"] = Field(
        "friendly", title="Тон", json_schema_extra={"labels": {
            "friendly": "дружелюбный деловой", "formal": "официальный", "concise": "максимально кратко",
            "enthusiastic": "с энтузиазмом"}})
    length: Literal["short", "medium", "long"] = Field(
        "short", title="Длина", json_schema_extra={"labels": {
            "short": "короткое (отклик на hh.ru)", "medium": "среднее", "long": "длинное"}})
    language: str = Field("", title="Язык письма (пусто = язык вакансии)")
    emphasis: str = Field("", title="Что подчеркнуть / доп. контекст")
    # Auto-apply sends letters unseen: keep private wishes (salary floor etc.) out of the prompt.
    use_preferences: bool = Field(True, title="Учитывать мои пожелания к работе")


def _context(session, user_id, resume, vacancy, params) -> dict:
    """Reuse the latest match analysis (if any) so the letter hits the right points."""
    return {"match": latest_output(session, user_id, "match", resume_id=resume.id, vacancy_id=vacancy.id)}


FEATURE = register(
    AnalysisFeature(
        kind="cover_letter",
        title="Сопроводительное письмо",
        description="Персональное письмо под вакансию на основе резюме (и анализа соответствия, если он есть).",
        subject="resume_vacancy",
        task=LLMTask(name="cover_letter", version="9", output=CoverLetter,
                     template_dir=feature_dir(__file__), max_tokens=8000),
        params_model=CoverLetterParams,
        context_builder=_context,
        order=40,
    )
)
