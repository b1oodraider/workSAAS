"""Periodic digests: new good matches (and, later, follow-up reminders) pushed to linked chats."""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.bot.api import TelegramAPI, TelegramError, button, esc, keyboard
from app.bot.texts import salary, web_url
from app.core.config import get_settings
from app.core.db import session_scope, utcnow
from app.models import Analysis, User, UserVacancy, Vacancy

log = logging.getLogger(__name__)

MAX_ITEMS = 8


def _pending_matches(s, user: User, threshold: int) -> list[tuple[UserVacancy, Vacancy, Analysis]]:
    latest = (
        select(Analysis.vacancy_id, func.max(Analysis.id).label("aid"))
        .where(Analysis.user_id == user.id, Analysis.kind == "match")
        .group_by(Analysis.vacancy_id).subquery()
    )
    rows = s.execute(
        select(UserVacancy, Vacancy, Analysis)
        .join(Vacancy, Vacancy.id == UserVacancy.vacancy_id)
        .join(latest, latest.c.vacancy_id == Vacancy.id)
        .join(Analysis, Analysis.id == latest.c.aid)
        .where(UserVacancy.user_id == user.id, UserVacancy.notified_at.is_(None),
               UserVacancy.status.in_(["new", "saved"]), Analysis.score >= threshold)
        .order_by(Analysis.score.desc())
    ).all()
    return list(rows)


def build_digest(rows) -> tuple[str, dict]:
    lines = [f"🔥 <b>Новые подходящие вакансии: {len(rows)}</b>"]
    kb = []
    for i, (_uv, v, a) in enumerate(rows[:MAX_ITEMS], 1):
        lines.append(
            f"\n{i}. <b>{esc(int(a.score or 0))}</b> — "
            f'<a href="{esc(web_url(f"/vacancies/{v.id}"))}">{esc(v.title)}</a>\n'
            f"{esc(v.company or '')} · {esc(salary(v))}\n<i>{esc((a.output or {}).get('summary', ''))[:300]}</i>"
        )
        kb.append([button(f"✉️ №{i}", f"cl:{v.id}"), button(f"⭐ №{i}", f"st:saved:{v.id}"),
                   button(f"🙈 №{i}", f"st:hidden:{v.id}")])
    if len(rows) > MAX_ITEMS:
        lines.append(f"\n…и ещё {len(rows) - MAX_ITEMS}: /top")
    return "\n".join(lines), keyboard(*kb)


REMINDER_TEXTS = {
    "applied": "Прошла неделя с отклика, а ответа нет. Напомнить о себе?",
    "interview": "Напоминание по собеседованию",
    "offer": "Напоминание по офферу",
}


async def send_reminders(api: TelegramAPI) -> int:
    """Tracker reminders: next_action_at has come (e.g. a week without a reply)."""
    now = utcnow()
    sent = 0
    with session_scope() as s:
        rows = s.execute(
            select(UserVacancy, Vacancy, User)
            .join(Vacancy, Vacancy.id == UserVacancy.vacancy_id)
            .join(User, User.id == UserVacancy.user_id)
            .where(User.telegram_chat_id.is_not(None), User.is_active.is_(True),
                   UserVacancy.next_action_at <= now, UserVacancy.reminded_at.is_(None),
                   UserVacancy.status.in_(["applied", "interview", "offer"]))
            .order_by(UserVacancy.next_action_at).limit(50)
        ).all()
        plans = []
        for uv, v, user in rows:
            head = uv.next_action_note or REMINDER_TEXTS.get(uv.status.value, "Напоминание")
            text = (f"⏰ {esc(head)}\n\n<b>{esc(v.title)}</b> · {esc(v.company or '')}\n"
                    f'<a href="{esc(web_url(f"/vacancies/{v.id}"))}">карточка вакансии</a>')
            kb = keyboard(
                [button("✉️ Follow-up письмо", f"fu:{v.id}")],
                [button("⏰ Через неделю", f"sn:{v.id}"), button("❌ Отказ", f"st:rejected:{v.id}")],
            )
            plans.append((user.telegram_chat_id, text, kb, uv.id))
    for chat_id, text, kb, uv_id in plans:
        try:
            await api.send(chat_id, text, reply_markup=kb)
        except TelegramError as exc:
            log.warning("reminder to %s failed: %s", chat_id, exc)
            continue
        with session_scope() as s:
            s.get(UserVacancy, uv_id).reminded_at = utcnow()
        sent += 1
    return sent


async def send_digests(api: TelegramAPI) -> int:
    default_threshold = get_settings().telegram.notify_min_score
    sent = 0
    with session_scope() as s:
        users = list(s.scalars(select(User).where(User.telegram_chat_id.is_not(None), User.is_active.is_(True))))
        plans = []
        for user in users:
            threshold = user.notify_min_score if user.notify_min_score is not None else default_threshold
            if threshold > 100:
                continue
            rows = _pending_matches(s, user, threshold)
            if rows:
                text, kb = build_digest(rows)
                plans.append((user.telegram_chat_id, text, kb, [uv.id for uv, _, _ in rows]))
    for chat_id, text, kb, uv_ids in plans:
        try:
            await api.send(chat_id, text, reply_markup=kb)
        except TelegramError as exc:
            log.warning("digest to %s failed: %s", chat_id, exc)
            continue
        with session_scope() as s:
            for uv in s.scalars(select(UserVacancy).where(UserVacancy.id.in_(uv_ids))):
                uv.notified_at = utcnow()
        sent += 1
    return sent
