from typing import Literal

from pydantic import BaseModel, Field

from app.features import register
from app.features._shared import pair_context
from app.features.base import AnalysisFeature, feature_dir
from app.features.interview_prep.schema import InterviewPrep
from app.llm.tasks import LLMTask


class Params(BaseModel):
    stage: Literal["hr_screen", "technical", "final"] = Field("technical", title="Этап")
    focus: str = Field("", title="На что сделать упор (необязательно)")


FEATURE = register(
    AnalysisFeature(
        kind="interview_prep",
        title="Подготовка к интервью",
        description="Вероятные вопросы с планом ответа по вашему опыту, темы для повторения, вопросы работодателю.",
        subject="resume_vacancy",
        task=LLMTask(name="interview_prep", version="2", output=InterviewPrep,
                     template_dir=feature_dir(__file__), max_tokens=10000),
        params_model=Params,
        context_builder=lambda s, uid, r, v, p: pair_context(s, uid, r, v, "match", "vacancy_review"),
        order=50,
        group="more",
    )
)
