from pydantic import BaseModel, Field

from app.features import register
from app.features._shared import latest_output
from app.features.base import AnalysisFeature, feature_dir
from app.features.letter_critic.schema import LetterCritique
from app.llm.tasks import LLMTask
from app.services.errors import ValidationFailed


class Params(BaseModel):
    letter_text: str = Field("", title="Текст письма (пусто — проверить последнее сгенерированное)",
                             json_schema_extra={"widget": "textarea"})


def _context(s, user_id, resume, vacancy, params) -> dict:
    letter = params.letter_text.strip()
    if not letter:
        generated = latest_output(s, user_id, "cover_letter", resume_id=resume.id, vacancy_id=vacancy.id)
        letter = (generated or {}).get("body", "")
    if not letter:
        raise ValidationFailed("Вставьте текст письма или сначала сгенерируйте сопроводительное")
    return {"letter": letter}


FEATURE = register(
    AnalysisFeature(
        kind="letter_critic",
        title="Проверить письмо",
        description="Критик: разбор вашего (или сгенерированного) письма глазами рекрутера и исправленная версия.",
        subject="resume_vacancy",
        task=LLMTask(name="letter_critic", version="2", output=LetterCritique,
                     template_dir=feature_dir(__file__), max_tokens=8000),
        params_model=Params,
        context_builder=_context,
        score_field="score",
        order=45,
        group="more",
    )
)
