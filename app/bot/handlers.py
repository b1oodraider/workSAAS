"""Update dispatcher. Add a command with @command, a button handler with @callback."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.bot.api import TelegramAPI, TelegramError, button, clip, esc, keyboard, url_button
from app.bot.texts import VERDICTS, salary, web_url
from app.core.db import session_scope
from app.jobs import enqueue
from app.models import Resume, SavedSearch, User
from app.services import matches as matches_svc
from app.services import search as search_svc
from app.services import telegram_links
from app.services import usage as usage_svc
from app.services import users as users_svc
from app.services import vacancies as vacancy_svc
from app.services.errors import NotFound, ValidationFailed

log = logging.getLogger(__name__)


@dataclass
class Ctx:
    api: TelegramAPI
    chat_id: int
    user_id: int | None
    username: str | None
    args: str = ""
    callback_id: str | None = None
    message_id: int | None = None
    markup: dict[str, Any] | None = None

    async def reply(self, text: str, markup: dict[str, Any] | None = None) -> None:
        await self.api.send(self.chat_id, text, reply_markup=markup)


Handler = Callable[[Ctx], Awaitable[None]]


@dataclass
class Command:
    fn: Handler
    description: str
    public: bool  # available before the account is linked


COMMANDS: dict[str, Command] = {}
CALLBACKS: dict[str, Handler] = {}


def command(name: str, description: str, *, public: bool = False) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        COMMANDS[name] = Command(fn, description, public)
        return fn
    return deco


def callback(prefix: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        CALLBACKS[prefix] = fn
        return fn
    return deco


def menu_commands() -> list[tuple[str, str]]:
    return [(name, c.description) for name, c in COMMANDS.items()]


# --------------------------------------------------------------------------- dispatch


async def handle_update(api: TelegramAPI, update: dict[str, Any]) -> None:
    if "callback_query" in update:
        await _handle_callback(api, update["callback_query"])
    elif "message" in update:
        await _handle_message(api, update["message"])


def _linked_user_id(chat_id: int) -> int | None:
    with session_scope() as s:
        user = telegram_links.user_for_chat(s, chat_id)
        return user.id if user else None


async def _handle_message(api: TelegramAPI, msg: dict[str, Any]) -> None:
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return  # the bot works only in private chats
    chat_id = chat["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    ctx = Ctx(api=api, chat_id=chat_id, user_id=_linked_user_id(chat_id),
              username=(msg.get("from") or {}).get("username"))
    if text.startswith("/"):
        name, _, args = text[1:].partition(" ")
        name = name.split("@", 1)[0].lower()
        cmd = COMMANDS.get(name)
        ctx.args = args.strip()
        if cmd is None:
            await ctx.reply("Не знаю такой команды. /help — список команд.")
        elif not cmd.public and ctx.user_id is None:
            await _not_linked(ctx)
        else:
            await cmd.fn(ctx)
        return
    if ctx.user_id is None:
        await _not_linked(ctx)
        return
    ctx.args = text
    await on_vacancy_text(ctx)


async def _handle_callback(api: TelegramAPI, cq: dict[str, Any]) -> None:
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
    data = str(cq.get("data") or "")
    prefix, _, args = data.partition(":")
    message = cq.get("message") or {}
    ctx = Ctx(api=api, chat_id=chat_id, user_id=_linked_user_id(chat_id) if chat_id else None,
              username=(cq.get("from") or {}).get("username"), args=args, callback_id=cq.get("id"),
              message_id=message.get("message_id"), markup=message.get("reply_markup"))
    handler = CALLBACKS.get(prefix)
    if ctx.user_id is None or handler is None:
        await api.answer_callback(cq["id"], "Аккаунт не привязан" if ctx.user_id is None else "Устаревшая кнопка")
        return
    try:
        await handler(ctx)
    except (NotFound, ValidationFailed, ValueError):
        await api.answer_callback(cq["id"], "Не найдено")
    except Exception:
        # Always stop the button spinner, then let the runner log the error.
        await api.answer_callback(cq["id"], "Ошибка, попробуйте позже")
        raise


async def _not_linked(ctx: Ctx) -> None:
    await ctx.reply(
        "Бот ещё не привязан к аккаунту.\n"
        f'Откройте <a href="{esc(web_url("/settings"))}">Настройки</a> в веб-интерфейсе, '
        "нажмите «Привязать Telegram» и перейдите по ссылке (или отправьте сюда /start КОД)."
    )


# --------------------------------------------------------------------------- commands


@command("start", "Начало работы", public=True)
async def cmd_start(ctx: Ctx) -> None:
    if ctx.args:
        with session_scope() as s:
            user = telegram_links.link_chat(s, ctx.args, ctx.chat_id, ctx.username)
            name = user.username if user else None
        if name is None:
            await ctx.reply("Код недействителен или устарел. Получите новый в Настройках веб-интерфейса.")
            return
        await ctx.reply(f"Готово, чат привязан к аккаунту <b>{esc(name)}</b>.\n\n" + help_text())
        return
    if ctx.user_id is None:
        await _not_linked(ctx)
    else:
        await ctx.reply(help_text())


HELP_INTRO = (
    "Что я умею:\n"
    "• пришлите <b>ссылку на вакансию</b> (hh.ru, Хабр Карьера или любой сайт) или <b>её текст</b> — "
    "оценю вакансию и соответствие вашему резюме, напишу сопроводительное;\n"
    "• присылаю новые подходящие вакансии из ваших поисков и напоминаю о follow-up.\n"
)


def help_text() -> str:
    """Built from the command registry, so new commands appear automatically."""
    lines = [f"/{name} — {c.description}" for name, c in COMMANDS.items() if name not in ("start", "help")]
    return HELP_INTRO + "\n" + "\n".join(lines)


@command("help", "Что умеет бот", public=True)
async def cmd_help(ctx: Ctx) -> None:
    await ctx.reply(help_text())


@command("top", "лучшие совпадения")
async def cmd_top(ctx: Ctx) -> None:
    with session_scope() as s:
        rows = [(v, a) for _, v, a in matches_svc.top_matches(s, ctx.user_id, limit=8)]
        if not rows:
            await ctx.reply("Пока нет оценённых вакансий. Запустите поиск: /searches")
            return
        lines, kb = ["<b>Лучшие совпадения</b>"], []
        for i, (v, a) in enumerate(rows, 1):
            lines.append(f"{i}. <b>{esc(int(a.score or 0))}</b> — "
                         f'<a href="{esc(web_url(f"/vacancies/{v.id}"))}">{esc(clip(v.title, 120))}</a>'
                         f" · {esc(clip(v.company or '', 80))} · {esc(salary(v))}")
            kb.append([button(f"✉️ Письмо №{i}", f"cl:{v.id}"), button(f"🙈 Скрыть №{i}", f"st:hidden:{v.id}")])
    await ctx.reply("\n".join(lines), keyboard(*kb))


@command("searches", "поиски и их запуск")
async def cmd_searches(ctx: Ctx) -> None:
    with session_scope() as s:
        searches = list(s.scalars(select(SavedSearch).where(SavedSearch.user_id == ctx.user_id)))
        if not searches:
            await ctx.reply("Поисков пока нет.", keyboard([url_button("Создать поиск", web_url("/searches"))]))
            return
        kb = [[button(f"▶️ {s_.name[:40]}", f"run:{s_.id}")] for s_ in searches[:10]]
    await ctx.reply("Какой поиск запустить? Новые подходящие вакансии пришлю сюда.", keyboard(*kb))


@command("resumes", "выбрать резюме для бота")
async def cmd_resumes(ctx: Ctx) -> None:
    with session_scope() as s:
        user = s.get(User, ctx.user_id)
        current = telegram_links.default_resume(s, user)
        resumes = list(s.scalars(select(Resume).where(Resume.user_id == ctx.user_id).order_by(Resume.id.desc())))
        if not resumes:
            await ctx.reply("Резюме пока нет.", keyboard([url_button("Загрузить резюме", web_url("/resumes"))]))
            return
        kb = [[button(("✅ " if current and r.id == current.id else "") + r.title[:50], f"res:{r.id}")]
              for r in resumes[:10]]
    await ctx.reply("По какому резюме оценивать вакансии и писать письма?", keyboard(*kb))


@command("notify", "порог уведомлений о вакансиях")
async def cmd_notify(ctx: Ctx) -> None:
    arg = ctx.args.lower()
    with session_scope() as s:
        if arg in ("off", "выкл", "нет"):
            users_svc.set_notify_threshold(s, ctx.user_id, users_svc.NOTIFY_OFF)
            msg = "Уведомления о вакансиях выключены. Включить: /notify 75"
        elif arg.isdigit() and 0 <= int(arg) <= 100:
            users_svc.set_notify_threshold(s, ctx.user_id, int(arg))
            msg = f"Буду присылать вакансии с оценкой от {int(arg)}."
        else:
            current = users_svc.notify_threshold(s.get(User, ctx.user_id))
            msg = None
    if msg is None:
        await ctx.reply(f"Присылать вакансии с оценкой от… Сейчас: {'выкл' if current > 100 else current}.",
                        keyboard([button(str(v), f"nt:{v}") for v in (60, 70, 80, 90)],
                                 [button("Не присылать", "nt:off")]))
        return
    await ctx.reply(msg)


@callback("nt")
async def cb_notify(ctx: Ctx) -> None:
    value = users_svc.NOTIFY_OFF if ctx.args == "off" else _int_arg(ctx.args)
    with session_scope() as s:
        users_svc.set_notify_threshold(s, ctx.user_id, value)
    await ctx.api.answer_callback(ctx.callback_id, "Выключено" if value > 100 else f"Порог: {value}")


@command("usage", "расходы на ИИ в этом месяце")
async def cmd_usage(ctx: Ctx) -> None:
    spent, budget = usage_svc.spent(ctx.user_id), usage_svc.budget(ctx.user_id)
    limit = f" из ${budget:.2f}" if budget > 0 else " (без лимита)"
    await ctx.reply(f"В этом месяце потрачено ${spent:.2f}{limit}.")


@command("unlink", "отвязать этот чат")
async def cmd_unlink(ctx: Ctx) -> None:
    with session_scope() as s:
        telegram_links.unlink(s, ctx.user_id)
    await ctx.reply("Чат отвязан. Уведомлений больше не будет.")


# --------------------------------------------------------------------------- free text

_URL_RE = re.compile(r"https?://\S+")


async def on_vacancy_text(ctx: Ctx) -> None:
    text = ctx.args
    m = _URL_RE.search(text)
    if m and len(text) < 300:
        payload = {"chat_id": ctx.chat_id, "url": m.group(0).rstrip(").,")}
    elif len(text) >= 120:
        payload = {"chat_id": ctx.chat_id, "text": text[:vacancy_svc.MAX_VACANCY_CHARS]}
    else:
        await ctx.reply("Пришлите ссылку на вакансию или её полный текст. /help — что я умею.")
        return
    enqueue("bot_vacancy", payload, user_id=ctx.user_id, title="Анализ вакансии из Telegram")
    await ctx.reply("Принял, анализирую… (обычно до минуты)")


# --------------------------------------------------------------------------- buttons


def _int_arg(value: str) -> int:
    if not value.isdigit():
        raise ValueError(value)
    return int(value)


@callback("cl")
async def cb_letter(ctx: Ctx) -> None:
    vacancy_id = _int_arg(ctx.args)
    with session_scope() as s:
        vacancy_svc.get_for_user(s, ctx.user_id, vacancy_id)  # ownership check
    enqueue("bot_letter", {"chat_id": ctx.chat_id, "vacancy_id": vacancy_id},
            user_id=ctx.user_id, title="Сопроводительное из Telegram")
    await ctx.api.answer_callback(ctx.callback_id, "Пишу письмо…")


@callback("fu")
async def cb_follow_up(ctx: Ctx) -> None:
    vacancy_id = _int_arg(ctx.args)
    with session_scope() as s:
        vacancy_svc.get_for_user(s, ctx.user_id, vacancy_id)
    enqueue("bot_letter", {"chat_id": ctx.chat_id, "vacancy_id": vacancy_id, "kind": "follow_up",
                           "params": {"situation": "no_reply"}},
            user_id=ctx.user_id, title="Follow-up из Telegram")
    await ctx.api.answer_callback(ctx.callback_id, "Пишу follow-up…")


@callback("sn")
async def cb_snooze(ctx: Ctx) -> None:
    with session_scope() as s:
        vacancy_svc.snooze(s, ctx.user_id, _int_arg(ctx.args), days=7)
    await ctx.api.answer_callback(ctx.callback_id, "Напомню через неделю")


STATUS_DONE = {"hidden": "🙈 Скрыто", "saved": "⭐ Сохранено", "applied": "✅ Откликнулся",
               "rejected": "❌ Отказ", "new": "Возвращено"}


@callback("st")
async def cb_status(ctx: Ctx) -> None:
    status, _, vid = ctx.args.partition(":")
    with session_scope() as s:
        vacancy_svc.set_status(s, ctx.user_id, _int_arg(vid), status)
    await ctx.api.answer_callback(ctx.callback_id, STATUS_DONE.get(status, "Готово"))
    if ctx.message_id and ctx.markup:
        # Mark the pressed vacancy in the message keyboard and offer an undo.
        rows = []
        for row in ctx.markup.get("inline_keyboard", []):
            if any(b.get("callback_data", "").endswith(f":{vid}") for b in row):
                rows.append([button(f"{STATUS_DONE.get(status, 'Готово')} · вернуть", f"st:new:{vid}")])
            else:
                rows.append(row)
        try:
            await ctx.api.edit_markup(ctx.chat_id, ctx.message_id, {"inline_keyboard": rows})
        except TelegramError:
            pass  # message too old to edit — the status is saved anyway


@callback("run")
async def cb_run_search(ctx: Ctx) -> None:
    search_id = _int_arg(ctx.args)
    with session_scope() as s:
        search_svc.get_owned(s, ctx.user_id, search_id)
    search_svc.enqueue_search_run(search_id, ctx.user_id)
    await ctx.api.answer_callback(ctx.callback_id, "Запустил поиск")
    await ctx.reply("Поиск запущен. Подходящие вакансии пришлю, как только ИИ их оценит.")


@callback("res")
async def cb_default_resume(ctx: Ctx) -> None:
    resume_id = _int_arg(ctx.args)
    with session_scope() as s:
        title = users_svc.set_default_resume(s, ctx.user_id, resume_id)
    await ctx.api.answer_callback(ctx.callback_id, "Выбрано")
    await ctx.reply(f"Теперь использую резюме «{esc(title)}».")


def vacancy_keyboard(vacancy_id: int) -> dict[str, Any]:
    return keyboard(
        [button("✉️ Сопроводительное", f"cl:{vacancy_id}")],
        [button("⭐ Сохранить", f"st:saved:{vacancy_id}"), button("✅ Откликнулся", f"st:applied:{vacancy_id}"),
         button("🙈 Скрыть", f"st:hidden:{vacancy_id}")],
    )


__all__ = ["COMMANDS", "CALLBACKS", "VERDICTS", "handle_update", "menu_commands", "vacancy_keyboard"]
