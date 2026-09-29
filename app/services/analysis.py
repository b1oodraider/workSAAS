"""Generic runner for every AnalysisFeature (see app/features)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import session_scope
from app.features import AnalysisFeature, get_feature
from app.jobs import JobContext, enqueue, job_handler
from app.llm import get_gateway
from app.models import Analysis, Resume, Vacancy
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


async def run_analysis(
    user_id: int,
    kind: str,
    *,
    resume_id: int | None = None,
    vacancy_id: int | None = None,
    params: dict[str, Any] | None = None,
) -> Analysis:
    feature = get_feature(kind)
    parsed = parse_params(feature, params or {})
    if feature.needs_vacancy and vacancy_id is not None:
        await vacancy_svc.ensure_full(vacancy_id)

    with session_scope() as s:
        resume, vacancy = _load_subjects(s, feature, user_id, resume_id, vacancy_id)
        variables: dict[str, Any] = {
            "params": parsed,
            "resume": resume.text if resume else "",
            "preferences": resume.preferences if resume else "",
            "vacancy": vacancy_svc.to_prompt(vacancy) if vacancy else "",
        }
        if feature.context_builder:
            variables.update(feature.context_builder(s, user_id, resume, vacancy, parsed))

    result = await get_gateway().run(feature.task, variables, user_id=user_id)
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
