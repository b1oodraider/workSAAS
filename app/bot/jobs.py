"""Background jobs started from the bot. They report results back to the chat themselves."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import update

from app.bot.api import TelegramError, esc, get_api
from app.bot.handlers import vacancy_keyboard
from app.bot.texts import vacancy_card
from app.core.db import session_scope, utcnow
from app.jobs import JobContext, job_handler
from app.jobs.queue import JobError
from app.llm import LLMError
from app.models import User, UserVacancy, Vacancy
from app.services import analysis as analysis_svc
from app.services import telegram_links
from app.services import vacancies as vacancy_svc
from app.services.errors import NotFound, ValidationFailed

log = logging.getLogger(__name__)


async def _notify(chat_id: int, text: str, markup: dict[str, Any] | None = None) -> None:
    api = get_api()
    if api is None:
        return
    try:
        await api.send(chat_id, text, reply_markup=markup)
    except TelegramError as exc:
        log.warning("telegram send failed: %s", exc)


def _default_resume_id(user_id: int) -> int | None:
    with session_scope() as s:
        user = s.get(User, user_id)
        resume = telegram_links.default_resume(s, user) if user else None
        return resume.id if resume else None


async def _run(ctx: JobContext, body) -> dict:
    """Common error handling: tell the user in the chat, then fail the job."""
    chat_id = ctx.payload["chat_id"]
    try:
        return await body()
    except (JobError, LLMError, NotFound, ValidationFailed) as exc:
        await _notify(chat_id, f"Не получилось: {esc(exc) or 'объект не найден'}")
        if isinstance(exc, (NotFound, ValidationFailed)):
            raise JobError(str(exc) or "Не найдено") from exc
        raise


@job_handler("bot_vacancy")
async def bot_vacancy(ctx: JobContext) -> dict:
    async def body() -> dict:
        p = ctx.payload
        if p.get("url"):
            draft = await vacancy_svc.draft_from_url(p["url"])
            with session_scope() as s:
                vacancy_id = vacancy_svc.import_for_user(s, ctx.user_id, draft).id
        else:
            with session_scope() as s:
                vacancy_id = vacancy_svc.create_manual(s, ctx.user_id, title="", text=p["text"]).id

        review = await analysis_svc.run_analysis(ctx.user_id, "vacancy_review", vacancy_id=vacancy_id)
        resume_id = _default_resume_id(ctx.user_id)
        match = None
        if resume_id:
            match = await analysis_svc.run_analysis(ctx.user_id, "match", resume_id=resume_id,
                                                    vacancy_id=vacancy_id)
        with session_scope() as s:
            vacancy = s.get(Vacancy, vacancy_id)
            text = vacancy_card(vacancy, match.output if match else None, review.output)
            s.execute(update(UserVacancy)
                      .where(UserVacancy.user_id == ctx.user_id, UserVacancy.vacancy_id == vacancy_id)
                      .values(notified_at=utcnow()))
        if not resume_id:
            text += "\n\nЗагрузите резюме в веб-интерфейсе — тогда я оценю и соответствие."
        await _notify(p["chat_id"], text, vacancy_keyboard(vacancy_id))
        return {"vacancy_id": vacancy_id, "result_url": f"/vacancies/{vacancy_id}"}

    return await _run(ctx, body)


@job_handler("bot_letter")
async def bot_letter(ctx: JobContext) -> dict:
    async def body() -> dict:
        vacancy_id = int(ctx.payload["vacancy_id"])
        resume_id = _default_resume_id(ctx.user_id)
        if resume_id is None:
            raise JobError("нет резюме — загрузите его в веб-интерфейсе")
        a = await analysis_svc.run_analysis(ctx.user_id, "cover_letter", resume_id=resume_id,
                                            vacancy_id=vacancy_id)
        o = a.output
        parts = [f"✉️ <b>{esc(o.get('subject', ''))}</b>", f"<pre>{esc(o.get('body', ''))}</pre>"]
        if o.get("warnings"):
            parts.append("Перед отправкой:\n" + "\n".join(f"• {esc(w)}" for w in o["warnings"][:5]))
        await _notify(ctx.payload["chat_id"], "\n\n".join(parts), vacancy_keyboard(vacancy_id))
        return {"analysis_id": a.id, "result_url": f"/analyses/{a.id}"}

    return await _run(ctx, body)
