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

from sqlalchemy import select, update

from app.bot.api import MAX_TEXT, TelegramAPI, TelegramError, button, clip, esc, keyboard, url_button
from app.bot.texts import autoapply_pause_keyboard, salary, web_url
from app.core.db import session_scope, utcnow
from app.models import Analysis, User, UserVacancy, Vacancy
from app.services import autoapply as autoapply_svc
from app.services import matches as matches_svc
from app.services import users as users_svc

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
        kb.append([button(f"{i}. ✉️ Письмо", f"cl:{v.id}"), button(f"{i}. ⭐ Сохранить", f"st:saved:{v.id}"),
                   button(f"{i}. 🙈", f"st:hidden:{v.id}")])
    lines = [f"🔥 <b>Новые подходящие вакансии: {len(rows)}</b>", *items]
    if len(rows) > len(ids):
        lines.append(f"\n…и ещё {len(rows) - len(ids)} — смотрите /top")
    return "\n".join(lines), keyboard(*kb), ids


async def send_digests(api: TelegramAPI) -> int:
    sent = 0
    with session_scope() as s:
        users = list(s.scalars(select(User).where(User.telegram_chat_id.is_not(None), User.is_active.is_(True))))
        plans = []
        for user in users:
            threshold = users_svc.notify_threshold(user)
            if threshold > 100:
                continue
            rows = matches_svc.top_matches(s, user.id, min_score=threshold, unnotified=True)
            if rows:
                plans.append((user.telegram_chat_id, rows))
        messages = []
        for chat_id, rows in plans:
            text, kb, ids = build_digest(rows)
            messages.append((chat_id, text, kb, ids, [uv.id for uv, _, _ in rows if uv.id not in ids]))
    for chat_id, text, kb, ids, rest_ids in messages:
        won = _claim(UserVacancy.notified_at, ids, utcnow())
        if len(won) != len(ids):  # another process is sending this digest
            _release(UserVacancy.notified_at, won)
            continue
        if await _deliver(api, chat_id, text, kb, UserVacancy.notified_at, won):
            sent += 1
            # Don't drip a backlog one message per interval: the rest is available via /top.
            if rest_ids:
                _claim(UserVacancy.notified_at, rest_ids, utcnow())
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
            kb.append([button(f"{i}. ✉️ Follow-up", f"fu:{vid}"), button(f"{i}. ⏰ Через неделю", f"sn:{vid}"),
                       button(f"{i}. ❌ Отказ", f"st:rejected:{vid}")])
        ids = [it[0] for it in items]
        won = _claim(UserVacancy.reminded_at, ids, now)
        if len(won) != len(ids):  # another process is sending these reminders
            _release(UserVacancy.reminded_at, won)
            continue
        if await _deliver(api, chat_id, "\n".join(lines), keyboard(*kb), UserVacancy.reminded_at, won):
            sent += 1
    return sent


# --------------------------------------------------------------------------- auto-apply

APPLY_RESULT_ICONS = {"applied": "✅", "skipped": "⏭", "failed": "⚠️"}


def site_link(url: str | None) -> str:
    """Vacancy URLs come from third-party feeds: only http(s) links go into messages."""
    return url if url and url.lower().startswith(("http://", "https://")) else ""


def _confirm_message(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    head = "✉️ <b>Проверьте письмо перед отправкой</b>" if item["review"] else "🤖 <b>Откликнуться?</b>"
    link = esc(web_url(f"/vacancies/{item['vid']}"))
    lines = [head, f'<b>{item["score"]}</b> — <a href="{link}">{esc(item["title"])}</a>',
             " · ".join(esc(x) for x in (item["company"], item["salary"]) if x)]
    if item["reason"]:
        lines.append(f"⚠️ {esc(item['reason'])}")
    lines.append(f"\n<b>Письмо:</b>\n{esc(item['letter'])}")
    rows = [[button("✅ Откликнуться", f"aa:{item['id']}"), button("❌ Не откликаться", f"ax:{item['id']}")]]
    if item["url"]:
        rows.append([url_button("Вакансия на сайте", item["url"])])
    return "\n".join(line for line in lines if line), keyboard(*rows)


async def send_autoapply_updates(api: TelegramAPI) -> int:
    """Letters to confirm, results of sent applications and pause notices."""
    sent = 0
    with session_scope() as s:
        chats = list(s.execute(select(User.id, User.telegram_chat_id)
                               .where(User.telegram_chat_id.is_not(None), User.is_active.is_(True))).all())
    for user_id, chat_id in chats:
        with session_scope() as s:
            upd = autoapply_svc.pending_updates(s, user_id)
            confirm = [{"id": a.id, "vid": a.vacancy.id, "title": clip(a.vacancy.title, 100),
                        "company": clip(a.vacancy.company or "", 60), "salary": salary(a.vacancy),
                        "score": int(a.score or 0), "letter": clip(a.letter, 2000),
                        "reason": clip(a.reason, 300) if a.status.value == "review" else "",
                        "review": a.status.value == "review", "url": site_link(a.vacancy.url)}
                       for a in upd["to_confirm"]]
            results = [(a.id, a.status.value, clip(a.vacancy.title, 100), clip(a.reason, 200),
                        site_link(a.vacancy.url)) for a in upd["results"]]
        pause_note = autoapply_svc.claim_pause_notice(user_id)
        if pause_note:
            can_resume = any(r["ok"] for r in autoapply_svc.site_readiness(user_id).values())
            try:
                await api.send(chat_id, f"⏸ <b>Автоотклики на паузе</b>\n{esc(clip(pause_note, 500))}",
                               reply_markup=autoapply_pause_keyboard(can_resume))
                sent += 1
            except TelegramError as exc:
                log.warning("pause notice to %s failed: %s", chat_id, exc)
                if not exc.permanent:
                    autoapply_svc.release_pause_notice(user_id)
        if confirm:
            ids = autoapply_svc.claim_for_notification([c["id"] for c in confirm])
            for item in (c for c in confirm if c["id"] in ids):
                text, kb = _confirm_message(item)
                try:
                    await api.send(chat_id, text, reply_markup=kb)
                    sent += 1
                except TelegramError as exc:
                    log.warning("confirm request to %s failed: %s", chat_id, exc)
                    if not exc.permanent:
                        autoapply_svc.release_notification([item["id"]])
        if results:
            ids = autoapply_svc.claim_for_notification([r[0] for r in results])
            items = [r for r in results if r[0] in ids]
            if items:
                lines = ["📨 <b>Автоотклики</b>"]
                for _app_id, status, title, reason, url in items:
                    icon = APPLY_RESULT_ICONS.get(status, "•")
                    name = f'<a href="{esc(url)}">{esc(title)}</a>' if url else esc(title)
                    lines.append(f"{icon} {name}" + (f" — <i>{esc(reason)}</i>" if status != "applied" else ""))
                try:
                    await api.send(chat_id, "\n".join(lines),
                                   reply_markup=keyboard([url_button("Подробнее и письма", web_url("/autoapply"))]))
                    sent += 1
                except TelegramError as exc:
                    log.warning("apply report to %s failed: %s", chat_id, exc)
                    if not exc.permanent:
                        autoapply_svc.release_notification(ids)
    return sent
