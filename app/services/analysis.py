"""Generic runner for every AnalysisFeature (see app/features)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import session_scope
from app.features import AnalysisFeature, get_feature
from app.jobs import JobContext, enqueue, job_handler
from app.core.config import LLMRoute
from app.llm import TaskResult, get_gateway
from app.models import Analysis, Resume, Vacancy
from app.services import criteria as criteria_svc
from app.services import resumes as resume_svc
from app.services import vacancies as vacancy_svc
from app.services.errors import NotFound, ValidationFailed


def parse_params(feature: AnalysisFeature, raw: dict[str, Any]) -> BaseModel:
    try:
        return feature.params_model.model_validate(raw)
    except ValidationError as exc:
        err = exc.errors()[0]
        name = str(err["loc"][0]) if err.get("loc") else ""
        field = feature.params_model.model_fields.get(name)
        title = (field.title if field else None) or name
        if err.get("type") == "missing":
            raise ValidationFailed(f"Заполните поле «{title}»") from exc
        if err.get("type") == "string_too_long":
            limit = (err.get("ctx") or {}).get("max_length")
            raise ValidationFailed(f"Поле «{title}»: не длиннее {limit} символов") from exc
        raise ValidationFailed(f"Поле «{title}»: {err['msg']}") from exc


def _load_subjects(
    s: Session, feature: AnalysisFeature, user_id: int, resume_id: int | None, vacancy_id: int | None
) -> tuple[Resume | None, Vacancy | None]:
    resume = vacancy = None
    if feature.needs_resume:
        if resume_id is None:
            raise ValidationFailed("Выберите резюме")
        resume = resume_svc.get_owned(s, user_id, resume_id)
    if feature.needs_vacancy:
        if vacancy_id is None:
            raise ValidationFailed("Выберите вакансию")
        vacancy, _ = vacancy_svc.get_for_user(s, user_id, vacancy_id)
    return resume, vacancy


def prompt_variables(s: Session, feature: AnalysisFeature, user_id: int, resume_id: int | None,
                     vacancy_id: int | None, parsed: BaseModel) -> dict[str, Any]:
    """Everything a feature's templates get (also used by the model comparison tool)."""
    resume, vacancy = _load_subjects(s, feature, user_id, resume_id, vacancy_id)
    variables: dict[str, Any] = {
        "params": parsed,
        "resume": resume.text if resume else "",
        "preferences": criteria_svc.preferences_text(resume, employer_facing=feature.employer_facing)
        if resume else "",
        "vacancy": vacancy_svc.to_prompt(vacancy) if vacancy else "",
    }
    if feature.context_builder:
        variables.update(feature.context_builder(s, user_id, resume, vacancy, parsed))
    return variables


def enqueue_analysis(
    user_id: int,
    kind: str,
    *,
    resume_id: int | None = None,
    vacancy_id: int | None = None,
    params: dict[str, Any] | None = None,
    parent_id: int | None = None,
) -> int:
    feature = get_feature(kind)
    parsed = parse_params(feature, params or {})
    with session_scope() as s:  # validate access before queueing
        resume, vacancy = _load_subjects(s, feature, user_id, resume_id, vacancy_id)
        label = " / ".join(x for x in (resume and resume.title, vacancy and vacancy.title) if x)
    return enqueue(
        "analysis",
        {"kind": kind, "resume_id": resume_id, "vacancy_id": vacancy_id,
         "params": parsed.model_dump(mode="json")},
        user_id=user_id,
        title=f"{feature.title}: {label}"[:300],
        parent_id=parent_id,
    )


async def generate(
    user_id: int,
    kind: str,
    *,
    resume_id: int | None = None,
    vacancy_id: int | None = None,
    params: dict[str, Any] | None = None,
    route: LLMRoute | None = None,
    use_cache: bool = True,
    bill_user: bool = True,
) -> tuple[TaskResult, BaseModel]:
    """Run a feature without saving the result. ``route`` swaps the model (model comparison);
    ``bill_user=False`` keeps an operator's test off the user's monthly budget."""
    feature = get_feature(kind)
    parsed = parse_params(feature, params or {})
    with session_scope() as s:  # access check before any network call
        _load_subjects(s, feature, user_id, resume_id, vacancy_id)
    if feature.needs_vacancy and vacancy_id is not None:
        await vacancy_svc.ensure_full(vacancy_id)
    with session_scope() as s:
        variables = prompt_variables(s, feature, user_id, resume_id, vacancy_id, parsed)
    result = await get_gateway().run(feature.task, variables, user_id=user_id if bill_user else None,
                                     use_cache=use_cache, route=route)
    return result, parsed


async def run_analysis(
    user_id: int,
    kind: str,
    *,
    resume_id: int | None = None,
    vacancy_id: int | None = None,
    params: dict[str, Any] | None = None,
) -> Analysis:
    feature = get_feature(kind)
    result, parsed = await generate(user_id, kind, resume_id=resume_id, vacancy_id=vacancy_id, params=params)
    output = result.output.model_dump(mode="json")
    score = output.get(feature.score_field) if feature.score_field else None

    with session_scope() as s:
        analysis = Analysis(
            user_id=user_id,
            kind=kind,
            resume_id=resume_id if feature.needs_resume else None,
            vacancy_id=vacancy_id if feature.needs_vacancy else None,
            params=parsed.model_dump(mode="json"),
            output=output,
            score=float(score) if isinstance(score, (int, float)) else None,
            provider=result.provider,
            model=result.model,
            prompt_version=result.prompt_version,
        )
        s.add(analysis)
        s.flush()
        s.expunge(analysis)
    return analysis


@job_handler("analysis")
async def _analysis_job(ctx: JobContext) -> dict:
    p = ctx.payload
    analysis = await run_analysis(
        ctx.user_id, p["kind"], resume_id=p.get("resume_id"),
        vacancy_id=p.get("vacancy_id"), params=p.get("params"),
    )
    return {"analysis_id": analysis.id, "result_url": f"/analyses/{analysis.id}"}


def latest(s: Session, user_id: int, kind: str, *, resume_id: int | None = None,
           vacancy_id: int | None = None) -> Analysis | None:
    q = select(Analysis).where(Analysis.user_id == user_id, Analysis.kind == kind)
    if resume_id is not None:
        q = q.where(Analysis.resume_id == resume_id)
    if vacancy_id is not None:
        q = q.where(Analysis.vacancy_id == vacancy_id)
    return s.scalar(q.order_by(Analysis.id.desc()).limit(1))


def get_owned(s: Session, user_id: int, analysis_id: int) -> Analysis:
    a = s.get(Analysis, analysis_id)
    if a is None or a.user_id != user_id:
        raise NotFound("analysis")
    return a
