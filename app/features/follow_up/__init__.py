from typing import Literal

from pydantic import BaseModel, Field

from app.features import register
from app.features._shared import pair_context
from app.features.base import AnalysisFeature, feature_dir
from app.features.follow_up.schema import FollowUp
from app.llm.tasks import LLMTask


class Params(BaseModel):
    situation: Literal["no_reply", "after_interview", "after_test_task", "after_rejection"] = Field(
        "no_reply", title="Ситуация", json_schema_extra={"labels": {
            "no_reply": "нет ответа на отклик", "after_interview": "после собеседования",
            "after_test_task": "после тестового", "after_rejection": "после отказа"}})
    details: str = Field("", title="Подробности (с кем общались, что обсуждали)",
                         json_schema_extra={"widget": "textarea"})


FEATURE = register(
    AnalysisFeature(
        kind="follow_up",
        title="Follow-up письмо",
        description="Напомнить о себе после тишины, поблагодарить после интервью или попросить обратную связь после отказа.",
        subject="resume_vacancy",
        task=LLMTask(name="follow_up", version="5", output=FollowUp,
                     template_dir=feature_dir(__file__), max_tokens=6000),
        params_model=Params,
        context_builder=lambda s, uid, r, v, p: pair_context(s, uid, r, v, "match"),
        order=60,
        group="more",
    )
)
