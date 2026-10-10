"""Vacancy matching pipeline: profile -> fetch from sources -> prefilter -> LLM match top-N."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer, joinedload

from app.core.config import get_settings
from app.core.db import session_scope, utcnow
from app.features import get_feature
from app.features.resume_profile.schema import ResumeProfile
from app.jobs import JobContext, enqueue, job_handler
from app.jobs.scheduler import periodic
from app.jobs.queue import JobError
from app.models import Analysis, Job, JobStatus, SavedSearch, User, UserVacancy, Vacancy
from app.ranking.prefilter import KeywordRanker, RankInput, RankProfile, Ranker, RankResult, normalize
from app.ranking.semantic import blend, blended_score
from app.services import analysis as analysis_svc
from app.services import criteria as criteria_svc
from app.services import matches as matches_svc
from app.services import resumes as resume_svc
from app.services import semantic as semantic_svc
from app.services import vacancies as vacancy_svc
from app.services.errors import NotFound, ValidationFailed
from app.sources import SearchFilters, SearchQuery, SourceError, get_source

log = logging.getLogger(__name__)

MAX_QUERIES = 8
ADJACENT_QUERIES = 2
MIN_INTERVAL_MINUTES = 60
MAX_SEARCHES_PER_USER = 20
# A full match beats a quick one for the same vacancy; both are scores of one resume.
MATCH_KINDS = ("match", "match_quick")


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
    count = s.scalar(select(func.count(SavedSearch.id)).where(SavedSearch.user_id == user_id))
    if count >= MAX_SEARCHES_PER_USER:
        raise ValidationFailed(f"Не больше {MAX_SEARCHES_PER_USER} сохранённых поисков — удалите лишние")
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
        # Automatic runs no more often than hourly: be polite to job sites.
        interval_minutes=0 if interval_minutes <= 0 else max(MIN_INTERVAL_MINUTES, interval_minutes),
    )
    s.add(search)
    s.flush()
    return search


@periodic
def enqueue_due_searches() -> list[int]:
    """Scheduler tick: enqueue runs of saved searches whose interval has passed."""
    now = utcnow()
    due: list[tuple[int, int]] = []
    with session_scope() as s:
        searches = s.scalars(
            select(SavedSearch).join(User, User.id == SavedSearch.user_id)
            .where(SavedSearch.enabled.is_(True), SavedSearch.interval_minutes > 0, User.is_active.is_(True))
        ).all()
        active = {
            (j.payload or {}).get("search_id")
            for j in s.scalars(
                select(Job).where(
                    Job.kind == "search_run", Job.status.in_([JobStatus.queued, JobStatus.running])
                )
            )
        }
        for search in searches:
            if search.id in active:
                continue
            if search.last_run_at and search.last_run_at + timedelta(minutes=search.interval_minutes) > now:
                continue
            due.append((search.id, search.user_id))
    return [enqueue_search_run(search_id, user_id) for search_id, user_id in due]


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


def to_rank_profile(profile: ResumeProfile, crit: criteria_svc.SearchCriteria | None = None) -> RankProfile:
    crit = crit or criteria_svc.SearchCriteria()
    return RankProfile(
        core_skills=[sk.name for sk in profile.skills if sk.level == "core"],
        secondary_skills=[sk.name for sk in profile.skills if sk.level == "secondary"],
        roles=[*profile.roles, *crit.roles_extra],
        # Related roles (same core skills) still earn part of the title score.
        adjacent_roles=profile.adjacent_roles if crit.adjacent_roles else [],
        negative_keywords=profile.negative_keywords,
    )


def build_queries(search: SavedSearch, profile: ResumeProfile,
                  crit: criteria_svc.SearchCriteria | None = None) -> list[str]:
    """The search's own queries, the user's extra roles, the profile's queries, then (if allowed)
    a couple of adjacent roles — whatever doesn't fit MAX_QUERIES is cut from the end."""
    crit = crit or criteria_svc.SearchCriteria()
    adjacent = profile.adjacent_roles[:ADJACENT_QUERIES] if crit.adjacent_roles else []
    own = profile.search_queries[:max(3, MAX_QUERIES - len(search.queries) - len(crit.roles_extra) - len(adjacent))]
    seen: set[str] = set()
    result: list[str] = []
    for q in [*search.queries, *crit.roles_extra, *own, *adjacent]:
        key = q.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(q.strip())
    return result[:MAX_QUERIES]


def _statuses(s: Session, user_id: int, vacancy_ids: list[int]) -> dict[int, str]:
    """Current status of the user's already attached vacancies (chunked for SQLite's parameter cap)."""
    found: dict[int, str] = {}
    for i in range(0, len(vacancy_ids), 500):
        found.update((vid, st.value) for vid, st in s.execute(
            select(UserVacancy.vacancy_id, UserVacancy.status).where(
                UserVacancy.user_id == user_id, UserVacancy.vacancy_id.in_(vacancy_ids[i : i + 500]))))
    return found


SOURCES_IN_PARALLEL = 4


async def _fetch_source(source, queries: list[str], filters: SearchFilters, limit: int,
                        gate: asyncio.Semaphore, errors: list[str]) -> list:
    drafts: list = []
    async with gate:
        for q in queries:
            try:
                drafts += await source.search(SearchQuery(text=q, filters=filters), limit)
            except SourceError as exc:
                errors.append(f"{source.title} «{q}»: {exc}")
            except Exception as exc:  # noqa: BLE001 - a parser bug in one source must not lose the others
                log.exception("source %s failed on %r", source.name, q)
                errors.append(f"{source.title} «{q}»: внутренняя ошибка ({type(exc).__name__})")
    return drafts


async def run_search(user_id: int, search_id: int, *, parent_job_id: int | None = None,
                     ranker: Ranker | None = None) -> dict[str, Any]:
    settings = get_settings()
    ranker = ranker or KeywordRanker()
    with session_scope() as s:
        search = get_owned(s, user_id, search_id)
        resume_id, source_names = search.resume_id, list(search.sources)
        crit = criteria_svc.get(s, user_id, resume_id)
        # The candidate's hard limits (salary floor, remote only…) tighten every search of the resume.
        filters = criteria_svc.effective_filters(SearchFilters.model_validate(search.filters or {}), crit)
        # Set before fetching: a failing run is not retried by the scheduler every minute.
        search.last_run_at = utcnow()

    profile = await get_profile(user_id, resume_id)
    with session_scope() as s:
        queries = build_queries(get_owned(s, user_id, search_id), profile, crit)

    # 1. Fetch from sources.
    errors: list[str] = []
    sources = []
    for name in source_names:
        try:
            source = get_source(name)
        except SourceError as exc:
            errors.append(str(exc))
            continue
        if source.searchable:
            sources.append(source)
    # Sources run side by side (each keeps its queries in order and its own per-host pauses):
    # one slow site (Telegram channels, scraped boards) must not hold the whole run for minutes.
    gate = asyncio.Semaphore(SOURCES_IN_PARALLEL)
    per_source = await asyncio.gather(*(_fetch_source(src, queries, filters, settings.matching.fetch_limit,
                                                      gate, errors) for src in sources))
    drafts = [d for batch in per_source for d in batch]
    if not drafts and errors:
        raise JobError("Источники недоступны: " + "; ".join(errors[:3]))

    # 2. Store + keyword prefilter (no network calls while the DB session is open).
    rank_profile = to_rank_profile(profile, crit)
    candidates: list[tuple[int, RankResult, str]] = []  # vacancy id, keyword rank, text to embed
    new_count = 0
    with session_scope() as s:
        seen_ids: set[int] = set()
        duplicates = 0
        skipped = 0
        for draft in drafts:
            # One bad row (a value the DB rejects, a ranker bug on odd text) is skipped: it must
            # never lose the whole run: rolling back to the savepoint drops only this draft's writes.
            # Not one atomic transaction, though: with pysqlite's default transaction handling the
            # SAVEPOINT itself may open the transaction, and its RELEASE then commits that draft at
            # once. Fine here, every stored vacancy is complete on its own.
            try:
                with s.begin_nested():
                    stored, created = vacancy_svc.upsert(s, draft)
                    # The same vacancy from another source: keep one entry, match it once.
                    vacancy = vacancy_svc.canonical_for(s, stored)
                    if vacancy.id in seen_ids:
                        duplicates += int(vacancy.id != stored.id)
                        continue
                    rank = ranker.score(
                        rank_profile,
                        RankInput(title=vacancy.title, text=vacancy.description + " " + " ".join(vacancy.skills),
                                  salary_from=vacancy.salary_from, salary_to=vacancy.salary_to,
                                  remote=vacancy.remote, currency=vacancy.currency, company=vacancy.company),
                        filters,
                    )
                    text = semantic_svc.vacancy_text(vacancy)
            except Exception:  # noqa: BLE001 - see above
                log.exception("search %s: skipped vacancy %s/%s", search_id, draft.source, draft.external_id)
                skipped += 1
                continue
            duplicates += int(vacancy.id != stored.id)
            seen_ids.add(vacancy.id)
            new_count += int(created and vacancy.id == stored.id)
            candidates.append((vacancy.id, rank, text))
        if skipped:
            errors.append(f"Пропущено вакансий с некорректными данными: {skipped}")
        resume_text = resume_svc.get_owned(s, user_id, resume_id).text
        status = _statuses(s, user_id, [vid for vid, _, _ in candidates])

    def is_open(vid: int) -> bool:  # not attached yet = new
        return status.get(vid, "new") in matches_svc.OPEN_STATUSES

    # 2b. Meaning similarity ([llm.embeddings]): finds what keywords miss — another language,
    # synonyms, related roles. Open vacancies only, best keyword scores first (max_per_run cap);
    # each text is embedded once for all users; any failure falls back to keywords.
    to_embed = sorted((c for c in candidates if not c[1].excluded and is_open(c[0])),
                      key=lambda c: c[1].score, reverse=True)
    similarity = await semantic_svc.similarities(
        user_id, semantic_svc.resume_query(profile, crit, resume_text),
        [(vid, text) for vid, _, text in to_embed], errors)
    weight = settings.llm.embeddings.weight
    scored: list[tuple[float, int]] = []
    with session_scope() as s:
        for vid, rank, _ in candidates:
            sim = similarity.get(vid)
            # prefilter_score stays keywords-only: auto-apply relies on it (skills found in the text).
            uv = vacancy_svc.attach(s, user_id, vid, search_id=search_id, prefilter_score=rank.score)
            uv.semantic_score = sim  # None clears a score from an earlier run
            # Hidden, already applied (here or on the site), rejected…: not worth paying the LLM for.
            if not rank.excluded and uv.status.value in matches_svc.OPEN_STATUSES:
                # Once similarities exist, a vacancy without one (over the cap) counts as 0 by meaning:
                # every candidate is ranked on the same scale.
                scored.append((blend(rank, sim if sim is not None or not similarity else 0.0, weight).score, vid))

        # 3. LLM for top-N not scored yet (for this resume and prompt version).
        quick = settings.matching.quick_match

        # Scores made before the resume or its search criteria changed are stale: score again.
        changed_at = resume_svc.get_owned(s, user_id, resume_id).updated_at

        def scored_by(kind: str, *extra) -> set[int]:
            return set(s.scalars(select(Analysis.vacancy_id).where(
                Analysis.user_id == user_id, Analysis.kind == kind, Analysis.resume_id == resume_id,
                Analysis.prompt_version == get_feature(kind).task.version,
                Analysis.created_at >= changed_at, *extra)))

        already = scored_by("match")
        if quick:  # a promising quick score without its full match yet is triaged again (no re-pay)
            already |= scored_by("match_quick", Analysis.score < settings.matching.full_match_min)
        # Still waiting in the queue (e.g. a re-run before the previous one finished).
        for job in s.scalars(select(Job).where(Job.user_id == user_id, Job.kind.in_(["analysis", "match_triage"]),
                                               Job.status.in_([JobStatus.queued, JobStatus.running]))):
            p = job.payload or {}
            if p.get("kind", "match") == "match" and p.get("resume_id") == resume_id:
                already.add(p.get("vacancy_id"))

    scored.sort(reverse=True)
    to_match = [vid for score, vid in scored
                if score >= settings.matching.prefilter_min and vid not in already]
    to_match = to_match[: settings.matching.top_n]
    for vid in to_match:
        if quick:
            enqueue_triage(user_id, resume_id, vid, parent_id=parent_job_id)
        else:
            analysis_svc.enqueue_analysis(user_id, "match", resume_id=resume_id, vacancy_id=vid,
                                          parent_id=parent_job_id)
    return {
        "queries": queries,
        "found": len(drafts),
        "unique": len(scored),
        "new": new_count,
        "duplicates": duplicates,
        "llm_match_enqueued": len(to_match),
        "errors": errors[:10],
        "result_url": f"/searches/{search_id}",
    }


def enqueue_triage(user_id: int, resume_id: int, vacancy_id: int, *, parent_id: int | None = None) -> int:
    with session_scope() as s:  # validate access before queueing
        resume = resume_svc.get_owned(s, user_id, resume_id)
        vacancy, _ = vacancy_svc.get_for_user(s, user_id, vacancy_id)
        title = f"Оценка: {resume.title} / {vacancy.title}"[:300]
    return enqueue("match_triage", {"resume_id": resume_id, "vacancy_id": vacancy_id},
                   user_id=user_id, title=title, parent_id=parent_id)


def _still_open(user_id: int, vacancy_id: int) -> bool:
    with session_scope() as s:
        status = s.scalar(select(UserVacancy.status).where(UserVacancy.user_id == user_id,
                                                           UserVacancy.vacancy_id == vacancy_id))
    return status is not None and status.value in matches_svc.OPEN_STATUSES


@job_handler("match_triage")
async def _triage_job(ctx: JobContext) -> dict:
    """Cheap score first; the detailed (pricier) match only for promising vacancies."""
    resume_id, vacancy_id = ctx.payload["resume_id"], ctx.payload["vacancy_id"]
    version = get_feature("match_quick").task.version
    with session_scope() as s:  # a retry after the full match failed must not pay twice
        quick = analysis_svc.latest(s, ctx.user_id, "match_quick", resume_id=resume_id, vacancy_id=vacancy_id)
        changed_at = resume_svc.get_owned(s, ctx.user_id, resume_id).updated_at
        if quick is not None and (quick.prompt_version != version or quick.created_at < changed_at):
            quick = None  # made for an older prompt, resume text or search criteria
        if quick is not None:
            s.expunge(quick)
    if quick is None:
        if not _still_open(ctx.user_id, vacancy_id):
            return {"skipped": "vacancy is no longer open"}
        quick = await analysis_svc.run_analysis(ctx.user_id, "match_quick", resume_id=resume_id,
                                                vacancy_id=vacancy_id)
    if quick.score is None or quick.score < get_settings().matching.full_match_min:
        return {"analysis_id": quick.id, "result_url": f"/analyses/{quick.id}"}
    if not _still_open(ctx.user_id, vacancy_id):  # hidden/applied while waiting: don't pay for it
        return {"analysis_id": quick.id, "result_url": f"/analyses/{quick.id}"}
    full = await analysis_svc.run_analysis(ctx.user_id, "match", resume_id=resume_id, vacancy_id=vacancy_id)
    return {"analysis_id": full.id, "result_url": f"/analyses/{full.id}"}


@job_handler("search_run")
async def _search_job(ctx: JobContext) -> dict:
    return await run_search(ctx.user_id, ctx.payload["search_id"], parent_job_id=ctx.job_id)


def results(s: Session, user_id: int, search_id: int, *, include_hidden: bool = False,
            limit: int = 200) -> list[dict[str, Any]]:
    """Vacancies of a search with prefilter score and latest LLM match (if any)."""
    search = get_owned(s, user_id, search_id)
    q = (
        select(UserVacancy)
        .where(UserVacancy.user_id == user_id, UserVacancy.search_id == search_id)
        # The list shows title/company/salary only: the long description and the source's raw
        # JSON stay in the DB (loaded on access if anything ever reads them).
        .options(joinedload(UserVacancy.vacancy, innerjoin=True).options(defer(Vacancy.description),
                                                                         defer(Vacancy.raw)))
    )
    if not include_hidden:
        q = q.where(UserVacancy.status != "hidden")
    rows = list(s.scalars(q))
    matches: dict[int, Analysis] = {}
    for a in s.scalars(
        select(Analysis)
        .where(Analysis.user_id == user_id, Analysis.kind.in_(MATCH_KINDS),
               Analysis.resume_id == search.resume_id)
        .order_by(Analysis.id)
    ):
        prev = matches.get(a.vacancy_id)
        if prev is None or a.kind == "match" or prev.kind == "match_quick":
            matches[a.vacancy_id] = a  # latest wins; a full match is never replaced by a quick one
    items = [{"uv": uv, "vacancy": uv.vacancy, "match": matches.get(uv.vacancy_id)} for uv in rows]
    weight = get_settings().llm.embeddings.weight
    items.sort(key=lambda it: (
        it["match"].score if it["match"] and it["match"].score is not None else -1,
        blended_score(it["uv"].prefilter_score or 0, it["uv"].semantic_score, weight),
    ), reverse=True)
    return items[:limit]


# --------------------------------------------------------------------------- hide with a reason

HIDE_REASONS = {
    "": "Просто скрыть",
    "company": "Не хочу в эту компанию",
    "level": "Не мой уровень (junior/senior/lead…)",
    "salary": "Мало платят",
    "office": "Нужна только удалёнка",
    "word": "Не моя область (впишу слово из названия)",
}
# Grade words looked up in the title for the "level" reason.
LEVEL_WORDS = ("стажер", "intern", "junior", "джуниор", "младший", "middle", "мидл", "senior", "сеньор",
               "старший", "ведущий", "lead", "тимлид", "teamlead", "head", "руководитель", "главный",
               "architect", "архитектор", "principal")
MAX_EXCLUSIONS = 100


def hide_with_reason(s: Session, user_id: int, vacancy_id: int, reason: str, word: str = "") -> str:
    """Hide a vacancy; the reason tunes the saved search that found it. Returns what changed.

    Only vacancies not acted on yet (new/saved) are hidden: an application in progress stays in
    the tracker, only the search filter changes."""
    if reason not in HIDE_REASONS:
        raise ValidationFailed("Неизвестная причина")
    vacancy, uv = vacancy_svc.get_for_user(s, user_id, vacancy_id)
    search = s.get(SavedSearch, uv.search_id) if uv.search_id and reason else None
    note, hidden_more = "", 0
    if search is not None and search.user_id == user_id:
        # Validate and change the filter first: an error must not leave the vacancy half-hidden.
        filters = SearchFilters.model_validate(search.filters or {})
        note = _tune(filters, vacancy, reason, word)
        if note and not note.startswith("("):
            search.filters = filters.model_dump()
            note += f" (поиск «{search.name}»)"
            if reason == "company":
                hidden_more = _hide_company(s, user_id, search.id, vacancy.company or "", vacancy.id)
    elif reason:
        note = "(она не из сохранённого поиска — фильтры не менялись)"
    if uv.status.value in matches_svc.OPEN_STATUSES:
        vacancy_svc.set_status(s, user_id, vacancy_id, "hidden")
        head = "Вакансия скрыта"
    else:
        head = "Статус вакансии в трекере не менялся"
    extra = f"; скрыто ещё {hidden_more} от этой компании" if hidden_more else ""
    if not note:
        return head
    return f"{head} {note}{extra}" if note.startswith("(") else f"{head}: {note}{extra}"


def _tune(filters: SearchFilters, vacancy: Vacancy, reason: str, word: str) -> str:
    """Change ``filters`` for the reason; returns what changed, or "(why nothing changed)"."""
    if reason == "company":
        if not vacancy.company:
            return "(компания не указана — фильтр не изменён)"
        _add(filters.exclude_companies, vacancy.company)
        return f"компания «{vacancy.company}» больше не попадёт в подбор"
    if reason == "level":
        title = normalize(vacancy.title)
        found = [w for w in LEVEL_WORDS if f" {w} " in title]
        if len(found) != 1:  # none, or "Middle/Senior": which one is wrong is the user's call
            return "(не понятно, какое слово про уровень исключить — впишите его через «Не моя область»)"
        _add(filters.exclude_title_words, found[0])
        return f"вакансии со словом «{found[0]}» в названии больше не попадут в подбор"
    if reason == "salary":
        if vacancy.currency not in ("", None, "RUR", "RUB") or not (vacancy.salary_to or vacancy.salary_from):
            return "(у неё нет зарплаты в рублях — фильтр не изменён)"
        # The middle of the range: "too little" rarely means "even the top is too little".
        low, high = vacancy.salary_from or vacancy.salary_to, vacancy.salary_to or vacancy.salary_from
        filters.salary_min = max(filters.salary_min or 0, (low + high) // 2 + 1)
        return f"минимальная зарплата в поиске — {filters.salary_min:,} ₽".replace(",", " ")
    if reason == "office":
        filters.remote_only = True
        return "в поиске теперь только удалёнка"
    word = " ".join(word.split())[:50]
    if len(word) < 2:
        raise ValidationFailed("Впишите слово из названия, которое не подходит")
    _add(filters.exclude_title_words, word)
    return f"вакансии со словом «{word}» в названии больше не попадут в подбор"


def _add(items: list[str], value: str) -> None:
    if value.lower() not in {i.lower() for i in items}:
        if len(items) >= MAX_EXCLUSIONS:
            raise ValidationFailed(f"В поиске уже {MAX_EXCLUSIONS} исключений — уберите лишние на странице поиска")
        items.append(value)


def _hide_company(s: Session, user_id: int, search_id: int, company: str, except_id: int) -> int:
    """Other still-unseen vacancies of the company in this search go too.
    Compared in Python: SQLite lower() folds only ASCII, so «Яндекс» would never match."""
    key = normalize(company).strip()
    count = 0
    for uv in s.scalars(select(UserVacancy).join(Vacancy).where(
            UserVacancy.user_id == user_id, UserVacancy.search_id == search_id,
            UserVacancy.status == "new", Vacancy.company.is_not(None), Vacancy.id != except_id)):
        if normalize(uv.vacancy.company or "").strip() == key:
            vacancy_svc.set_status(s, user_id, uv.vacancy_id, "hidden")
            count += 1
    return count


def remove_exclusion(s: Session, user_id: int, search_id: int, kind: str, value: str) -> None:
    search = get_owned(s, user_id, search_id)
    filters = SearchFilters.model_validate(search.filters or {})
    if kind == "remote_only":
        filters.remote_only = False
    elif kind == "salary_min":
        filters.salary_min = None
    elif kind in ("exclude_words", "exclude_title_words", "exclude_companies"):
        setattr(filters, kind, [v for v in getattr(filters, kind) if v != value])
    else:
        raise ValidationFailed("Неизвестный фильтр")
    search.filters = filters.model_dump()
