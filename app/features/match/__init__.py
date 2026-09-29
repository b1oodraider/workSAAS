from app.features import register
from app.features.base import AnalysisFeature, feature_dir
from app.features.match.schema import MatchResult
from app.llm.tasks import LLMTask

FEATURE = register(
    AnalysisFeature(
        kind="match",
        title="Соответствие резюме",
        description="Насколько резюме подходит под вакансию: оценка, совпадения, пробелы, что подчеркнуть.",
        subject="resume_vacancy",
        # Resume lives in the system prompt so bulk matching reuses the prompt cache.
        task=LLMTask(name="match", version="1", output=MatchResult,
                     template_dir=feature_dir(__file__), max_tokens=8000, cache_system=True),
        score_field="score",
        order=30,
    )
)
