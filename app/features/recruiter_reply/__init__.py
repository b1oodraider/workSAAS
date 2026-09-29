from pydantic import BaseModel, Field

from app.features import register
from app.features.base import AnalysisFeature, feature_dir
from app.features.recruiter_reply.schema import RecruiterReply
from app.llm.tasks import LLMTask


class Params(BaseModel):
    message: str = Field(title="Сообщение рекрутера", min_length=10,
                         json_schema_extra={"widget": "textarea", "placeholder": "Вставьте сообщение из hh.ru, Telegram, почты…"})
    goal: str = Field("", title="Чего хотите добиться (необязательно)")


FEATURE = register(
    AnalysisFeature(
        kind="recruiter_reply",
        title="Ответ рекрутеру",
        description="Черновик ответа на сообщение HR и ответы на скрининг-вопросы (ожидания, сроки, формат).",
        subject="resume_vacancy",
        task=LLMTask(name="recruiter_reply", version="2", output=RecruiterReply,
                     template_dir=feature_dir(__file__), max_tokens=6000),
        params_model=Params,
        order=65,
        group="more",
    )
)
