from app.features import register
from app.features.base import AnalysisFeature, feature_dir
from app.features.vacancy_review.schema import VacancyReview
from app.llm.tasks import LLMTask

FEATURE = register(
    AnalysisFeature(
        kind="vacancy_review",
        title="Оценка вакансии",
        description="Качество вакансии, красные и зелёные флаги, реальные требования, вопросы работодателю.",
        subject="vacancy",
        task=LLMTask(name="vacancy_review", version="1", output=VacancyReview,
                     template_dir=feature_dir(__file__)),
        score_field="overall_score",
        order=20,
    )
)
