from __future__ import annotations

import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import session_scope, utcnow
from app.core.http import make_async_client
from app.core.text import html_to_text
from app.jobs import JobContext, enqueue, job_handler
from app.jobs.queue import JobError
from app.models import UserVacancy, UserVacancyStatus, Vacancy
from app.services.errors import NotFound, ValidationFailed
from app.sources import SourceError, VacancyDraft, available_sources, get_source

MAX_VACANCY_CHARS = 40_000


def upsert(s: Session, draft: VacancyDraft) -> tuple[Vacancy, bool]:
    """Insert or refresh a vacancy. A partial draft never overwrites a full description."""
    vacancy = s.scalar(
        select(Vacancy).where(Vacancy.source == draft.source, Vacancy.external_id == draft.external_id)
    )
    data = draft.model_dump()
    created = vacancy is None
    if vacancy is None:
        vacancy = Vacancy(**data)
        s.add(vacancy)
    else:
        keep_full = draft.is_partial and not vacancy.is_partial
        for key, value in data.items():
            if keep_full and key in {"description", "is_partial", "raw", "skills"}:
                continue
            setattr(vacancy, key, value)
        vacancy.fetched_at = utcnow()
    s.flush()
    return vacancy, created


def attach(s: Session, user_id: int, vacancy_id: int, *, search_id: int | None = None,
           prefilter_score: float | None = None) -> UserVacancy:
    uv = s.scalar(select(UserVacancy).where(UserVacancy.user_id == user_id,
                                            UserVacancy.vacancy_id == vacancy_id))
    if uv is None:
        uv = UserVacancy(user_id=user_id, vacancy_id=vacancy_id)
        s.add(uv)
    if search_id is not None:
        uv.search_id = search_id
    if prefilter_score is not None:
        uv.prefilter_score = prefilter_score
    s.flush()
    return uv


def get_for_user(s: Session, user_id: int, vacancy_id: int) -> tuple[Vacancy, UserVacancy]:
    uv = s.scalar(select(UserVacancy).where(UserVacancy.user_id == user_id,
                                            UserVacancy.vacancy_id == vacancy_id))
    if uv is None:
        raise NotFound("vacancy")
    return uv.vacancy, uv


def set_status(s: Session, user_id: int, vacancy_id: int, status: str, notes: str | None = None) -> None:
    _, uv = get_for_user(s, user_id, vacancy_id)
    try:
        uv.status = UserVacancyStatus(status)
    except ValueError as exc:
        raise ValidationFailed("Неизвестный статус") from exc
    if notes is not None:
        uv.notes = notes


def to_prompt(v: Vacancy) -> str:
    """Vacancy -> text block for LLM prompts."""
    lines = [f"Должность: {v.title}"]
    if v.company:
        lines.append(f"Компания: {v.company}")
    if v.location:
        lines.append(f"Локация: {v.location}")
    if v.salary_from or v.salary_to:
        sal = " – ".join(str(x) for x in (v.salary_from, v.salary_to) if x)
        gross = {True: " (до вычета налогов)", False: " (на руки)"}.get(v.salary_gross, "")
        lines.append(f"Зарплата: {sal} {v.currency or ''}{gross}".rstrip())
    else:
        lines.append("Зарплата: не указана")
    if v.remote is not None:
        lines.append("Удалённая работа: " + ("да" if v.remote else "нет"))
    if v.experience:
        lines.append(f"Опыт: {v.experience}")
    if v.employment:
        lines.append(f"Занятость: {v.employment}")
    if v.skills:
        lines.append("Ключевые навыки: " + ", ".join(v.skills))
    lines.append("")
    lines.append(v.description or "(описание отсутствует)")
    if v.is_partial:
        lines.append("\n[Внимание: доступен только краткий фрагмент описания вакансии]")
    return "\n".join(lines)


async def ensure_full(vacancy_id: int) -> None:
    """Fetch the full card for vacancies stored from search snippets."""
    with session_scope() as s:
        v = s.get(Vacancy, vacancy_id)
        if v is None or not v.is_partial:
            return
        source_name, ext_id = v.source, v.external_id
    try:
        draft = await get_source(source_name).fetch(ext_id)
    except SourceError:
        return  # analysis proceeds on the snippet; the prompt says it's partial
    if draft:
        with session_scope() as s:
            upsert(s, draft)


def create_manual(s: Session, user_id: int, *, title: str, text: str, url: str = "",
                  company: str = "") -> Vacancy:
    text = text.strip()
    if len(text) < 50:
        raise ValidationFailed("Текст вакансии слишком короткий")
    if len(text) > MAX_VACANCY_CHARS:
        raise ValidationFailed(f"Текст вакансии длиннее {MAX_VACANCY_CHARS} символов")
    title = title.strip() or text.splitlines()[0][:200]
    draft = VacancyDraft(
        source="manual",
        external_id=hashlib.sha1(f"{user_id}:{title}:{text}".encode()).hexdigest()[:32],
        title=title,
        url=url.strip() or None,
        company=company.strip() or None,
        description=text,
    )
    vacancy, _ = upsert(s, draft)
    attach(s, user_id, vacancy.id)
    return vacancy


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_STRIP_RE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.I | re.S)


async def draft_from_url(url: str) -> VacancyDraft:
    for src in available_sources():
        ext_id = src.external_id_from_url(url)
        if ext_id:
            draft = await src.fetch(ext_id)
            if draft:
                return draft
    # Generic page: best-effort text extraction.
    async with make_async_client() as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise JobError(f"Не удалось загрузить страницу: {exc}") from exc
    html = resp.text
    m = _TITLE_RE.search(html)
    text = html_to_text(_STRIP_RE.sub("", html))
    if len(text) < 100:
        raise JobError("На странице не найден текст вакансии — вставьте его вручную")
    return VacancyDraft(
        source="manual",
        external_id=hashlib.sha1(url.encode()).hexdigest()[:32],
        title=html_to_text(m.group(1))[:300] if m else url,
        url=url,
        description=text[:MAX_VACANCY_CHARS],
    )


def enqueue_import(user_id: int, url: str) -> int:
    if not url.startswith(("http://", "https://")):
        raise ValidationFailed("Нужна ссылка, начинающаяся с http:// или https://")
    return enqueue("vacancy_import", {"url": url}, user_id=user_id, title=f"Импорт вакансии {url[:80]}")


@job_handler("vacancy_import")
async def _import_job(ctx: JobContext) -> dict:
    try:
        draft = await draft_from_url(ctx.payload["url"])
    except SourceError as exc:
        raise JobError(str(exc)) from exc
    with session_scope() as s:
        vacancy, _ = upsert(s, draft)
        attach(s, ctx.user_id, vacancy.id)
        vacancy_id = vacancy.id
    return {"vacancy_id": vacancy_id, "result_url": f"/vacancies/{vacancy_id}"}
