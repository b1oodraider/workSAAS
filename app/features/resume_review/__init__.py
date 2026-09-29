from pydantic import BaseModel, Field

from app.features import register
from app.features.base import AnalysisFeature, feature_dir
from app.features.resume_review.schema import ResumeReview
from app.llm.tasks import LLMTask


class ResumeReviewParams(BaseModel):
    target_role: str = Field("", title="Целевая роль (необязательно)")


FEATURE = register(
    AnalysisFeature(
        kind="resume_review",
        title="Оценка резюме",
        description="Разбор резюме глазами рекрутера: оценка, проблемы с исправлениями, ключевые слова.",
        subject="resume",
        task=LLMTask(name="resume_review", version="1", output=ResumeReview,
                     template_dir=feature_dir(__file__)),
        params_model=ResumeReviewParams,
        score_field="overall_score",
        order=10,
    )
)
