from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import logging

import httpx
from bs4 import BeautifulSoup

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import session_scope, utcnow
from app.core.http import make_async_client
from app.core.text import html_to_text
from app.jobs import JobContext, enqueue, job_handler
from app.jobs.queue import JobError
from app.models import MATCH_VOTE_REASONS, RESPONSE_QUALITY, UserVacancy, UserVacancyStatus, Vacancy
from app.services.errors import NotFound, ValidationFailed
from app.sources import VacancyDraft, available_sources, get_source
from app.sources.jsonld import draft_from_html

log = logging.getLogger(__name__)

MAX_VACANCY_CHARS = 40_000


_NOISE_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]|[^\w\s]+")
_COMPANY_FORMS_RE = re.compile(r"\b(ооо|ао|пао|зао|оао|ип|llc|inc|ltd|gmbh)\b")


def normalize_name(value: str) -> str:
    """Lowercase, no punctuation, brackets or legal form: 'ООО «Ромашка» (Москва)' -> 'ромашка'."""
    value = _NOISE_RE.sub(" ", value.lower().replace("ё", "е"))
    return " ".join(_COMPANY_FORMS_RE.sub(" ", value).split())


def make_dedup_key(company: str | None, title: str) -> str | None:
    """Same employer + same title (ignoring punctuation, brackets, legal form) = same vacancy."""
    if not company:
        return None
    c, t = normalize_name(company), normalize_name(title)
    if not c or not t:
        return None
    return hashlib.sha1(f"{c}|{t}".encode()).hexdigest()


def trusted_sources() -> list[str]:
    """Sources whose texts are published by the employers themselves (JobSource.trusted)."""
    from app.sources.registry import SOURCE_CLASSES

    return [name for name, cls in SOURCE_CLASSES.items() if cls.trusted]


def canonical_for(s: Session, vacancy: Vacancy) -> Vacancy:
    """The first stored vacancy from a trusted source with the same dedup key (itself if none).

    User-generated content (untrusted sources, see JobSource.trusted) can't become the
    shared canonical copy: otherwise anyone could plant a fake description under a real
    company's vacancy. Unknown source names are treated as untrusted."""
    if not vacancy.dedup_key:
        return vacancy
    first = s.scalar(
        select(Vacancy)
        .where(Vacancy.dedup_key == vacancy.dedup_key, Vacancy.source.in_(trusted_sources()))
        .order_by(Vacancy.id).limit(1)
    )
    return first or vacancy


def import_for_user(s: Session, user_id: int, draft: VacancyDraft) -> Vacancy:
    """Store an imported vacancy; generic page imports are private per user (keyed by user)."""
    if draft.source == "manual":
        ext = hashlib.sha1(f"{user_id}:{draft.external_id}".encode()).hexdigest()[:32]
        draft = draft.model_copy(update={"external_id": ext})
    vacancy, _ = upsert(s, draft)
    attach(s, user_id, vacancy.id)
    return vacancy


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
        vacancy.fetched_at = utcnow()
    else:
        keep_full = draft.is_partial and not vacancy.is_partial
        for key, value in data.items():
            if keep_full and key in {"description", "is_partial", "raw", "skills"}:
                continue
            setattr(vacancy, key, value)
        vacancy.fetched_at = utcnow()
    vacancy.dedup_key = make_dedup_key(vacancy.company, vacancy.title)
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


# After applying, remind about a follow-up if there is no answer in this many days.
FOLLOW_UP_AFTER = timedelta(days=7)
ACTIVE_STATUSES = (UserVacancyStatus.applied, UserVacancyStatus.interview, UserVacancyStatus.offer)


def set_status(s: Session, user_id: int, vacancy_id: int, status: str, notes: str | None = None) -> None:
    _, uv = get_for_user(s, user_id, vacancy_id)
    try:
        new = UserVacancyStatus(status)
    except ValueError as exc:
        raise ValidationFailed("Неизвестный статус") from exc
    if notes is not None:
        uv.notes = notes
    if new == uv.status:
        return
    now = utcnow()
    uv.status = new
    uv.status_history = [*(uv.status_history or []), {"status": new.value, "at": now.isoformat(timespec="seconds")}]
    if new == UserVacancyStatus.applied:
        uv.applied_at = uv.applied_at or now
        uv.next_action_at = now + FOLLOW_UP_AFTER
        uv.next_action_note = "Нет ответа? Напомнить о себе"
    elif new in (UserVacancyStatus.interview, UserVacancyStatus.offer):
        uv.next_action_at, uv.next_action_note = None, ""
    else:  # rejected, hidden, or back to new/saved: nothing to follow up on
        uv.next_action_at, uv.next_action_note = None, ""
    uv.reminded_at = None


def set_match_vote(s: Session, user_id: int, vacancy_id: int, vote: int | None, reason: str = "") -> None:
    """👍 (+1) / 👎 (-1) on the AI match; None clears it. A reason only makes sense for 👎."""
    if vote not in (1, -1, None):
        raise ValidationFailed("Оценка — 👍 или 👎")
    if reason and (vote != -1 or reason not in MATCH_VOTE_REASONS):
        raise ValidationFailed("Неизвестная причина")
    _, uv = get_for_user(s, user_id, vacancy_id)
    uv.match_vote, uv.match_vote_reason = vote, reason


def set_response_quality(s: Session, user_id: int, vacancy_id: int, quality: str) -> None:
    if quality and quality not in RESPONSE_QUALITY:
        raise ValidationFailed("Неизвестный тип ответа")
    _, uv = get_for_user(s, user_id, vacancy_id)
    uv.response_quality = quality


def set_next_action(s: Session, user_id: int, vacancy_id: int, when: datetime | None, note: str = "") -> None:
    _, uv = get_for_user(s, user_id, vacancy_id)
    uv.next_action_at = when
    uv.next_action_note = note.strip()[:300]
    uv.reminded_at = None


def snooze(s: Session, user_id: int, vacancy_id: int, days: int = 7) -> None:
    _, uv = get_for_user(s, user_id, vacancy_id)
    uv.next_action_at = utcnow() + timedelta(days=days)
    uv.reminded_at = None


def tracker(s: Session, user_id: int) -> dict:
    """Applications grouped by status + funnel numbers."""
    rows = list(s.scalars(
        select(UserVacancy).where(UserVacancy.user_id == user_id,
                                  UserVacancy.status.in_([st.value for st in
                                                          (UserVacancyStatus.saved, *ACTIVE_STATUSES,
                                                           UserVacancyStatus.rejected)]))
        .order_by(UserVacancy.next_action_at.is_(None), UserVacancy.next_action_at, UserVacancy.id.desc())
    ))
    columns: dict[str, list[UserVacancy]] = {st: [] for st in ("saved", "applied", "interview", "offer", "rejected")}
    for uv in rows:
        columns[uv.status.value].append(uv)

    def reached(status: str) -> int:
        return sum(1 for uv in rows if any(h.get("status") == status for h in uv.status_history or [])
                   or uv.status.value == status)

    # Rows created before history was tracked may have reached "interview" without an
    # "applied" record: every interview/offer implies an application.
    applied = max(reached("applied"), reached("interview"), reached("offer"))
    return {
        "columns": columns,
        "due": [uv for uv in rows if uv.next_action_at and uv.next_action_at <= utcnow()],
        "funnel": {
            "applied": applied,
            "interview": reached("interview"),
            "offer": reached("offer"),
            "rejected": reached("rejected"),
            "interview_rate": round(100 * reached("interview") / applied) if applied else None,
        },
    }


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
    except Exception:  # noqa: BLE001 - analysis proceeds on the snippet; the prompt says it's partial
        log.warning("could not fetch full vacancy %s/%s", source_name, ext_id, exc_info=True)
        return
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


MAX_PAGE_BYTES = 3 * 1024 * 1024
FETCH_TOTAL_TIMEOUT_S = 45


async def _ensure_public_host(url: str) -> None:
    """Block SSRF: users must not make the server fetch localhost/LAN/cloud-metadata addresses."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host or parts.scheme not in ("http", "https"):
        raise JobError("Некорректная ссылка")
    if parts.port not in (None, 80, 443):
        raise JobError("Разрешены только стандартные порты 80/443")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError as exc:
        raise JobError(f"Не удалось найти сайт {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise JobError("Ссылки на локальные и внутренние адреса запрещены")


async def draft_from_url(url: str) -> VacancyDraft:
    for src in available_sources():
        ext_id = src.external_id_from_url(url)
        if ext_id:
            draft = await src.fetch(ext_id)
            if draft:
                return draft
    # Generic page: best-effort text extraction.
    try:
        html = await asyncio.wait_for(_fetch_public_page(url), timeout=FETCH_TOTAL_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise JobError("Страница грузится слишком долго") from None
    return page_to_draft(html, url)


async def _fetch_public_page(url: str) -> str:
    """GET with a size cap; redirects are followed manually so every hop is checked
    against internal addresses (residual risk: DNS rebinding between check and connect)."""
    async with make_async_client(follow_redirects=False) as client:
        target = url
        for _ in range(5):
            await _ensure_public_host(target)
            try:
                async with client.stream("GET", target) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        target = str(resp.url.join(resp.headers["location"]))
                        continue
                    if resp.status_code >= 400:
                        raise JobError(f"Страница вернула ошибку {resp.status_code}")
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_PAGE_BYTES:
                            raise JobError("Страница слишком большая")
                        chunks.append(chunk)
                    return b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
            except httpx.HTTPError as exc:
                raise JobError(f"Не удалось загрузить страницу: {type(exc).__name__}") from exc
        raise JobError("Слишком много перенаправлений")


def page_to_draft(html: str, url: str) -> VacancyDraft:
    ext_id = hashlib.sha1(url.encode()).hexdigest()[:32]
    structured = draft_from_html(html, source="manual", external_id=ext_id, url=url)
    if structured and structured.description:
        return structured
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = html_to_text(str(soup.body or soup))
    if len(text) < 100:
        raise JobError("На странице не найден текст вакансии — вставьте его вручную")
    return VacancyDraft(
        source="manual",
        external_id=ext_id,
        title=(title or url)[:300],
        url=url,
        description=text[:MAX_VACANCY_CHARS],
    )


def enqueue_import(user_id: int, url: str) -> int:
    if not url.startswith(("http://", "https://")):
        raise ValidationFailed("Нужна ссылка, начинающаяся с http:// или https://")
    return enqueue("vacancy_import", {"url": url}, user_id=user_id, title=f"Импорт вакансии {url[:80]}")


@job_handler("vacancy_import")
async def _import_job(ctx: JobContext) -> dict:
    draft = await draft_from_url(ctx.payload["url"])
    with session_scope() as s:
        vacancy_id = import_for_user(s, ctx.user_id, draft).id
    return {"vacancy_id": vacancy_id, "result_url": f"/vacancies/{vacancy_id}"}
