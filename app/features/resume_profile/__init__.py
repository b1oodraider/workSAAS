from app.features import register
from app.features.base import AnalysisFeature, feature_dir
from app.features.resume_profile.schema import ResumeProfile
from app.llm.tasks import LLMTask

FEATURE = register(
    AnalysisFeature(
        kind="resume_profile",
        title="Профиль для поиска",
        description="Извлекает из резюме навыки, роли и поисковые запросы для подбора вакансий.",
        subject="resume",
        task=LLMTask(name="resume_profile", version="6", output=ResumeProfile,
                     template_dir=feature_dir(__file__), max_tokens=8000),
        order=90,
    )
)
