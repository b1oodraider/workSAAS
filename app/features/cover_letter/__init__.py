from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.features import register
from app.features.base import AnalysisFeature, feature_dir
from app.features.cover_letter.schema import CoverLetter
from app.llm.tasks import LLMTask
from app.models import Analysis


class CoverLetterParams(BaseModel):
    tone: Literal["friendly", "formal", "concise", "enthusiastic"] = Field("friendly", title="Тон")
    length: Literal["short", "medium", "long"] = Field("short", title="Длина")
    language: str = Field("", title="Язык письма (пусто = язык вакансии)")
    emphasis: str = Field("", title="Что подчеркнуть / доп. контекст")


def _context(session, user_id, resume, vacancy, params) -> dict:
    """Reuse the latest match analysis (if any) so the letter hits the right points."""
    match = session.scalar(
        select(Analysis)
        .where(Analysis.user_id == user_id, Analysis.kind == "match",
               Analysis.resume_id == resume.id, Analysis.vacancy_id == vacancy.id)
        .order_by(Analysis.id.desc())
        .limit(1)
    )
    return {"match": match.output if match else None}


FEATURE = register(
    AnalysisFeature(
        kind="cover_letter",
        title="Сопроводительное письмо",
        description="Персональное письмо под вакансию на основе резюме (и анализа соответствия, если он есть).",
        subject="resume_vacancy",
        task=LLMTask(name="cover_letter", version="2", output=CoverLetter,
                     template_dir=feature_dir(__file__), max_tokens=8000),
        params_model=CoverLetterParams,
        context_builder=_context,
        order=40,
    )
)
