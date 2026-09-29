from pydantic import BaseModel, Field

from app.features import register
from app.features.base import AnalysisFeature, feature_dir
from app.features.offer_negotiation.schema import OfferReview
from app.llm.tasks import LLMTask


class Params(BaseModel):
    offer: str = Field(title="Условия оффера", min_length=10,
                       json_schema_extra={"widget": "textarea",
                                          "placeholder": "Сумма, на руки/до вычета, бонусы, формат, испытательный срок, дата выхода…"})
    desired: str = Field("", title="Чего хотите вы (сумма, условия)")


FEATURE = register(
    AnalysisFeature(
        kind="offer_negotiation",
        title="Разбор оффера",
        description="Оценка условий, аргументы для торга из вашего опыта и готовый текст ответа.",
        subject="resume_vacancy",
        task=LLMTask(name="offer_negotiation", version="1", output=OfferReview,
                     template_dir=feature_dir(__file__), max_tokens=8000),
        params_model=Params,
        order=70,
        group="more",
    )
)
