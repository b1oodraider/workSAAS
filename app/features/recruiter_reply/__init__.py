from pydantic import BaseModel, Field

from app.features import register
from app.features.base import AnalysisFeature, feature_dir
from app.features.recruiter_reply.schema import RecruiterReply
from app.features._shared import LINE_TEXT_MAX, LONG_TEXT_MAX
from app.llm.tasks import LLMTask


class Params(BaseModel):
    message: str = Field(title="Сообщение рекрутера", min_length=10, max_length=LONG_TEXT_MAX,
                         json_schema_extra={"widget": "textarea", "placeholder": "Вставьте сообщение из hh.ru, Telegram, почты…"})
    goal: str = Field("", title="Чего хотите добиться (необязательно)", max_length=LINE_TEXT_MAX)


FEATURE = register(
    AnalysisFeature(
        kind="recruiter_reply",
        title="Ответ рекрутеру",
        description="Черновик ответа на сообщение HR и ответы на скрининг-вопросы (ожидания, сроки, формат).",
        subject="resume_vacancy",
        task=LLMTask(name="recruiter_reply", version="3", output=RecruiterReply,
                     template_dir=feature_dir(__file__), max_tokens=6000),
        params_model=Params,
        employer_facing=True,
        order=65,
        group="more",
    )
)
