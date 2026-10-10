from app.features import register
from app.features.base import AnalysisFeature, feature_dir
from app.features.match_quick.schema import QuickMatch
from app.llm.tasks import LLMTask

FEATURE = register(
    AnalysisFeature(
        kind="match_quick",
        title="Быстрая оценка",
        description="Дешёвая первичная оценка вакансии при подборе: балл и одна строка. "
                    "Подробный разбор («Соответствие резюме») — только для лучших.",
        subject="resume_vacancy",
        # Resume lives in the system prompt so bulk triage reuses the prompt cache.
        # 2000: reasoning models (gpt-oss) spend "thinking" tokens from this budget too, and a
        # truncated answer is paid for anyway. Non-reasoning models: lower it in the route.
        task=LLMTask(name="match_quick", version="4", output=QuickMatch,
                     template_dir=feature_dir(__file__), max_tokens=2000, cache_system=True),
        score_field="score",
        hidden=True,  # runs inside the search pipeline
        order=31,
    )
)
