"""Periodic pushes to linked chats: digests of new good matches and tracker reminders.

Rows are *claimed* (notified_at / reminded_at set with a conditional UPDATE) before
sending, so two processes running the bot never send the same item twice. A claim is
released again only on transient errors; permanent ones (bad request, bot blocked)
keep it so a broken message can't be retried forever.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update

from app.bot.api import MAX_TEXT, TelegramAPI, TelegramError, button, clip, esc, keyboard
from app.bot.texts import salary, web_url
from app.core.config import get_settings
from app.core.db import session_scope, utcnow
from app.models import Analysis, User, UserVacancy, Vacancy

log = logging.getLogger(__name__)

MAX_ITEMS = 8
MAX_REMINDERS = 5
# Leave room for the header/footer inside Telegram's 4096-char limit.
TEXT_BUDGET = MAX_TEXT - 300

REMINDER_TEXTS = {
    "applied": "Прошла неделя с отклика, а ответа нет. Напомнить о себе?",
    "interview": "Напоминание по собеседованию",
    "offer": "Напоминание по офферу",
}


def _claim(column, ids: list[int], stamp: datetime) -> list[int]:
    """Atomically mark rows as being sent; returns the ids this process won."""
    won = []
    with session_scope() as s:
        for uv_id in ids:
            res = s.execute(update(UserVacancy).where(UserVacancy.id == uv_id, column.is_(None))
                            .values({column.key: stamp}))
            if res.rowcount:
                won.append(uv_id)
    return won


def _release(column, ids: list[int]) -> None:
    with session_scope() as s:
        s.execute(update(UserVacancy).where(UserVacancy.id.in_(ids)).values({column.key: None}))


def _unlink_chat(chat_id: int) -> None:
    with session_scope() as s:
        for user in s.scalars(select(User).where(User.telegram_chat_id == chat_id)):
            user.telegram_chat_id = None
    log.info("chat %s blocked the bot: unlinked", chat_id)


async def _deliver(api: TelegramAPI, chat_id: int, text: str, kb: dict[str, Any], column,
                   ids: list[int]) -> bool:
    try:
        await api.send(chat_id, text, reply_markup=kb)
        return True
    except TelegramError as exc:
        log.warning("push to chat %s failed: %s", chat_id, exc)
        if exc.code == 403:
            _unlink_chat(chat_id)
        elif not exc.permanent:
            _release(column, ids)  # transient: try again next tick
        return False


# --------------------------------------------------------------------------- digests


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


def _digest_item(i: int, v: Vacancy, a: Analysis) -> str:
    summary = (a.output or {}).get("summary", "")
    return (f"\n{i}. <b>{esc(int(a.score or 0))}</b> — "
            f'<a href="{esc(web_url(f"/vacancies/{v.id}"))}">{esc(clip(v.title, 120))}</a>\n'
            f"{esc(clip(v.company or '', 80))} · {esc(salary(v))}\n<i>{esc(clip(summary, 280))}</i>")


def build_digest(rows) -> tuple[str, dict, list[int]]:
    """Message that always fits into one Telegram message; returns ids actually included."""
    items, kb, ids = [], [], []
    size = 0
    for i, (uv, v, a) in enumerate(rows[:MAX_ITEMS], 1):
        item = _digest_item(i, v, a)
        if size + len(item) > TEXT_BUDGET:
            break
        size += len(item)
        items.append(item)
        ids.append(uv.id)
        kb.append([button(f"✉️ №{i}", f"cl:{v.id}"), button(f"⭐ №{i}", f"st:saved:{v.id}"),
                   button(f"🙈 №{i}", f"st:hidden:{v.id}")])
    lines = [f"🔥 <b>Новые подходящие вакансии: {len(rows)}</b>", *items]
    if len(rows) > len(ids):
        lines.append(f"\n…и ещё {len(rows) - len(ids)} — пришлю в следующий раз или смотрите /top")
    return "\n".join(lines), keyboard(*kb), ids


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
                plans.append((user.telegram_chat_id, rows))
        messages = [(chat_id, *build_digest(rows)) for chat_id, rows in plans]
    for chat_id, text, kb, ids in messages:
        won = _claim(UserVacancy.notified_at, ids, utcnow())
        if len(won) != len(ids):  # another process is sending this digest
            _release(UserVacancy.notified_at, won)
            continue
        if await _deliver(api, chat_id, text, kb, UserVacancy.notified_at, won):
            sent += 1
    return sent


# --------------------------------------------------------------------------- reminders


async def send_reminders(api: TelegramAPI) -> int:
    """Tracker reminders (e.g. a week without a reply), one grouped message per user."""
    now = utcnow()
    with session_scope() as s:
        rows = s.execute(
            select(UserVacancy, Vacancy, User)
            .join(Vacancy, Vacancy.id == UserVacancy.vacancy_id)
            .join(User, User.id == UserVacancy.user_id)
            .where(User.telegram_chat_id.is_not(None), User.is_active.is_(True),
                   UserVacancy.next_action_at <= now, UserVacancy.reminded_at.is_(None),
                   UserVacancy.status.in_(["applied", "interview", "offer"]))
            .order_by(UserVacancy.next_action_at).limit(200)
        ).all()
        per_chat: dict[int, list] = defaultdict(list)
        for uv, v, user in rows:
            if len(per_chat[user.telegram_chat_id]) < MAX_REMINDERS:
                head = uv.next_action_note or REMINDER_TEXTS.get(uv.status.value, "Напоминание")
                per_chat[user.telegram_chat_id].append((uv.id, v.id, clip(head, 150), clip(v.title, 100),
                                                        clip(v.company or "", 60)))
    sent = 0
    for chat_id, items in per_chat.items():
        lines, kb = ["⏰ <b>Пора действовать</b>"], []
        for i, (_uv_id, vid, head, title, company) in enumerate(items, 1):
            lines.append(f'\n{i}. <a href="{esc(web_url(f"/vacancies/{vid}"))}">{esc(title)}</a>'
                         f"{' · ' + esc(company) if company else ''}\n{esc(head)}")
            kb.append([button(f"✉️ Follow-up №{i}", f"fu:{vid}"), button(f"⏰ +7 дн. №{i}", f"sn:{vid}"),
                       button(f"❌ Отказ №{i}", f"st:rejected:{vid}")])
        ids = [it[0] for it in items]
        won = _claim(UserVacancy.reminded_at, ids, now)
        if len(won) != len(ids):  # another process is sending these reminders
            _release(UserVacancy.reminded_at, won)
            continue
        if await _deliver(api, chat_id, "\n".join(lines), keyboard(*kb), UserVacancy.reminded_at, won):
            sent += 1
    return sent
