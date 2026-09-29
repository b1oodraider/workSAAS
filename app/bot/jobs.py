"""Background jobs started from the bot. They report results back to the chat themselves."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import update

from app.bot.api import TelegramError, clip, esc, get_api
from app.bot.handlers import vacancy_keyboard
from app.bot.texts import vacancy_card
from app.core.db import session_scope, utcnow
from app.core.errors import UserError
from app.jobs import JobContext, job_handler
from app.jobs.queue import JobError
from app.llm import BudgetExceeded, LLMError, LLMInvalidOutput, LLMRefusal, LLMUnavailable
from app.models import User, UserVacancy, Vacancy
from app.services import analysis as analysis_svc
from app.services import telegram_links
from app.services import vacancies as vacancy_svc
from app.services.errors import NotFound

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


def user_message(exc: Exception) -> str:
    """Russian text for the chat; raw provider/library messages may be English."""
    if isinstance(exc, BudgetExceeded):
        return str(exc)
    if isinstance(exc, LLMRefusal):
        return "модель отказалась отвечать на этот запрос."
    if isinstance(exc, LLMUnavailable):
        return "ИИ-провайдер временно недоступен, попробуйте через несколько минут."
    if isinstance(exc, LLMInvalidOutput):
        return "модель вернула некорректный ответ, попробуйте ещё раз."
    if isinstance(exc, LLMError):
        return "ошибка ИИ-провайдера — сообщите администратору."
    if isinstance(exc, NotFound):
        return "вакансия или резюме не найдены."
    return str(exc) or "неизвестная ошибка"


async def _run(ctx: JobContext, body) -> dict:
    """Common error handling: tell the user in the chat, then fail the job."""
    chat_id = ctx.payload["chat_id"]
    try:
        return await body()
    except UserError as exc:
        if exc.retryable and not ctx.final_attempt:
            raise  # the queue will retry; tell the user only if it finally fails
        await _notify(chat_id, "Не получилось: " + esc(clip(user_message(exc), 500)))
        raise
    except Exception:
        await _notify(chat_id, "Не получилось: внутренняя ошибка. Попробуйте ещё раз позже.")
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


# Features whose output has "subject" + "body" and can be sent as a ready message.
LETTER_KINDS = ("cover_letter", "follow_up")


@job_handler("bot_letter")
async def bot_letter(ctx: JobContext) -> dict:
    async def body() -> dict:
        vacancy_id = int(ctx.payload["vacancy_id"])
        kind = ctx.payload.get("kind", "cover_letter")
        if kind not in LETTER_KINDS:
            raise JobError(f"неизвестный тип письма {kind}")
        resume_id = _default_resume_id(ctx.user_id)
        if resume_id is None:
            raise JobError("нет резюме — загрузите его в веб-интерфейсе")
        a = await analysis_svc.run_analysis(ctx.user_id, kind, resume_id=resume_id,
                                            vacancy_id=vacancy_id, params=ctx.payload.get("params"))
        o = a.output
        parts = [f"✉️ <b>{esc(clip(o.get('subject', ''), 200))}</b>",
                 f"<pre>{esc(clip(o.get('body', ''), 3000))}</pre>"]
        if o.get("warnings"):
            parts.append("Перед отправкой:\n" + "\n".join(f"• {esc(clip(w, 150))}" for w in o["warnings"][:4]))
        await _notify(ctx.payload["chat_id"], "\n\n".join(parts), vacancy_keyboard(vacancy_id))
        return {"analysis_id": a.id, "result_url": f"/analyses/{a.id}"}

    return await _run(ctx, body)
