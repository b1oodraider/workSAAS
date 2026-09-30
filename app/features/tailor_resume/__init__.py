from app.features import register
from app.features._shared import pair_context
from app.features.base import AnalysisFeature, feature_dir
from app.features.tailor_resume.schema import TailoredResume
from app.llm.tasks import LLMTask

FEATURE = register(
    AnalysisFeature(
        kind="tailor_resume",
        title="Адаптировать резюме",
        description="Точечные правки резюме под вакансию: что переписать и почему, без выдуманного опыта.",
        subject="resume_vacancy",
        task=LLMTask(name="tailor_resume", version="5", output=TailoredResume,
                     template_dir=feature_dir(__file__), max_tokens=10000),
        context_builder=lambda s, uid, r, v, p: pair_context(s, uid, r, v, "match"),
        order=55,
        group="more",
    )
)
