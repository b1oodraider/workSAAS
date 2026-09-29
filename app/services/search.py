"""Vacancy matching pipeline: profile -> fetch from sources -> prefilter -> LLM match top-N."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import session_scope, utcnow
from app.features import get_feature
from app.features.resume_profile.schema import ResumeProfile
from app.jobs import JobContext, enqueue, job_handler
from app.jobs.queue import JobError
from app.models import Analysis, SavedSearch, UserVacancy, Vacancy
from app.ranking.prefilter import KeywordRanker, RankInput, RankProfile, Ranker
from app.services import analysis as analysis_svc
from app.services import resumes as resume_svc
from app.services import vacancies as vacancy_svc
from app.services.errors import NotFound, ValidationFailed
from app.sources import SearchFilters, SearchQuery, SourceError, get_source

log = logging.getLogger(__name__)

MAX_QUERIES = 6


def get_owned(s: Session, user_id: int, search_id: int) -> SavedSearch:
    search = s.get(SavedSearch, search_id)
    if search is None or search.user_id != user_id:
        raise NotFound("search")
    return search


def create(
    s: Session,
    user_id: int,
    *,
    resume_id: int,
    name: str,
    sources: list[str],
    queries: list[str],
    filters: dict[str, Any],
    interval_minutes: int = 0,
) -> SavedSearch:
    resume = resume_svc.get_owned(s, user_id, resume_id)
    if not sources:
        raise ValidationFailed("Выберите хотя бы один источник")
    for name_ in sources:
        try:
            get_source(name_)
        except SourceError as exc:
            raise ValidationFailed(str(exc)) from exc
    search = SavedSearch(
        user_id=user_id,
        resume_id=resume.id,
        name=name.strip() or f"Поиск по «{resume.title}»",
        sources=sources,
        queries=[q.strip() for q in queries if q.strip()],
        filters=SearchFilters.model_validate(filters).model_dump(),
        interval_minutes=max(0, interval_minutes),
    )
    s.add(search)
    s.flush()
    return search


def enqueue_search_run(search_id: int, user_id: int) -> int:
    return enqueue("search_run", {"search_id": search_id}, user_id=user_id,
                   title="Подбор вакансий", result_url=f"/searches/{search_id}")


async def get_profile(user_id: int, resume_id: int) -> ResumeProfile:
    """Latest profile of the current prompt version, recomputed if the resume changed."""
    feature = get_feature("resume_profile")
    with session_scope() as s:
        resume = resume_svc.get_owned(s, user_id, resume_id)
        existing = analysis_svc.latest(s, user_id, "resume_profile", resume_id=resume_id)
        if (
            existing is not None
            and existing.prompt_version == feature.task.version
            and existing.created_at >= resume.updated_at
        ):
            return ResumeProfile.model_validate(existing.output)
    analysis = await analysis_svc.run_analysis(user_id, "resume_profile", resume_id=resume_id)
    return ResumeProfile.model_validate(analysis.output)


def to_rank_profile(profile: ResumeProfile) -> RankProfile:
    return RankProfile(
        core_skills=[sk.name for sk in profile.skills if sk.level == "core"],
        secondary_skills=[sk.name for sk in profile.skills if sk.level == "secondary"],
        roles=profile.roles,
        negative_keywords=profile.negative_keywords,
    )


def build_queries(search: SavedSearch, profile: ResumeProfile) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for q in [*search.queries, *profile.search_queries]:
        key = q.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(q.strip())
    return result[:MAX_QUERIES]


async def run_search(user_id: int, search_id: int, *, parent_job_id: int | None = None,
                     ranker: Ranker | None = None) -> dict[str, Any]:
    settings = get_settings()
    ranker = ranker or KeywordRanker()
    with session_scope() as s:
        search = get_owned(s, user_id, search_id)
        resume_id, source_names = search.resume_id, list(search.sources)
        filters = SearchFilters.model_validate(search.filters or {})

    profile = await get_profile(user_id, resume_id)
    with session_scope() as s:
        queries = build_queries(get_owned(s, user_id, search_id), profile)

    # 1. Fetch from sources.
    errors: list[str] = []
    drafts = []
    for name in source_names:
        try:
            source = get_source(name)
        except SourceError as exc:
            errors.append(str(exc))
            continue
        if not source.searchable:
            continue
        for q in queries:
            try:
                drafts += await source.search(SearchQuery(text=q, filters=filters),
                                              settings.matching.fetch_limit)
            except SourceError as exc:
                errors.append(f"{name} «{q}»: {exc}")
    if not drafts and errors:
        raise JobError("Источники недоступны: " + "; ".join(errors[:3]))

    # 2. Store + prefilter.
    rank_profile = to_rank_profile(profile)
    scored: list[tuple[float, int]] = []
    new_count = 0
    with session_scope() as s:
        seen_ids: set[int] = set()
        for draft in drafts:
            vacancy, created = vacancy_svc.upsert(s, draft)
            if vacancy.id in seen_ids:
                continue
            seen_ids.add(vacancy.id)
            new_count += int(created)
            rank = ranker.score(
                rank_profile,
                RankInput(title=vacancy.title, text=vacancy.description + " " + " ".join(vacancy.skills),
                          salary_from=vacancy.salary_from, salary_to=vacancy.salary_to,
                          remote=vacancy.remote, currency=vacancy.currency),
                filters,
            )
            uv = vacancy_svc.attach(s, user_id, vacancy.id, search_id=search_id,
                                    prefilter_score=rank.score)
            if not rank.excluded and uv.status.value != "hidden":
                scored.append((rank.score, vacancy.id))

        # 3. LLM match for top-N not matched yet (for this resume and prompt version).
        match_version = get_feature("match").task.version
        already = set(
            s.scalars(
                select(Analysis.vacancy_id).where(
                    Analysis.user_id == user_id, Analysis.kind == "match",
                    Analysis.resume_id == resume_id, Analysis.prompt_version == match_version,
                )
            )
        )
        s.get(SavedSearch, search_id).last_run_at = utcnow()

    scored.sort(reverse=True)
    to_match = [vid for score, vid in scored
                if score >= settings.matching.prefilter_min and vid not in already]
    to_match = to_match[: settings.matching.top_n]
    for vid in to_match:
        analysis_svc.enqueue_analysis(user_id, "match", resume_id=resume_id, vacancy_id=vid,
                                      parent_id=parent_job_id)
    return {
        "queries": queries,
        "found": len(drafts),
        "unique": len(scored),
        "new": new_count,
        "llm_match_enqueued": len(to_match),
        "errors": errors[:10],
        "result_url": f"/searches/{search_id}",
    }


@job_handler("search_run")
async def _search_job(ctx: JobContext) -> dict:
    try:
        return await run_search(ctx.user_id, ctx.payload["search_id"], parent_job_id=ctx.job_id)
    except (NotFound, ValidationFailed) as exc:
        raise JobError(str(exc) or "Поиск не найден") from exc


def results(s: Session, user_id: int, search_id: int, *, include_hidden: bool = False,
            limit: int = 200) -> list[dict[str, Any]]:
    """Vacancies of a search with prefilter score and latest LLM match (if any)."""
    search = get_owned(s, user_id, search_id)
    q = (
        select(UserVacancy)
        .where(UserVacancy.user_id == user_id, UserVacancy.search_id == search_id)
        .join(Vacancy)
    )
    if not include_hidden:
        q = q.where(UserVacancy.status != "hidden")
    rows = list(s.scalars(q))
    matches: dict[int, Analysis] = {}
    for a in s.scalars(
        select(Analysis)
        .where(Analysis.user_id == user_id, Analysis.kind == "match",
               Analysis.resume_id == search.resume_id)
        .order_by(Analysis.id)
    ):
        matches[a.vacancy_id] = a  # later rows overwrite -> latest wins
    items = [{"uv": uv, "vacancy": uv.vacancy, "match": matches.get(uv.vacancy_id)} for uv in rows]
    items.sort(key=lambda it: (
        it["match"].score if it["match"] and it["match"].score is not None else -1,
        it["uv"].prefilter_score or 0,
    ), reverse=True)
    return items[:limit]
