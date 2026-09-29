"""Update dispatcher. Add a command with @command, a button handler with @callback."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from app.bot.api import TelegramAPI, button, esc, keyboard, url_button
from app.bot.texts import VERDICTS, salary, web_url
from app.core.config import get_settings
from app.core.db import session_scope
from app.jobs import enqueue
from app.llm import get_gateway
from app.models import Analysis, Resume, SavedSearch, User, UserVacancy, Vacancy
from app.services import search as search_svc
from app.services import telegram_links
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
    ctx = Ctx(api=api, chat_id=chat_id, user_id=_linked_user_id(chat_id) if chat_id else None,
              username=(cq.get("from") or {}).get("username"), args=args, callback_id=cq.get("id"))
    handler = CALLBACKS.get(prefix)
    if ctx.user_id is None or handler is None:
        await api.answer_callback(cq["id"], "Аккаунт не привязан" if ctx.user_id is None else "Устаревшая кнопка")
        return
    try:
        await handler(ctx)
    except (NotFound, ValidationFailed, ValueError):
        await api.answer_callback(cq["id"], "Не найдено")


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
        await ctx.reply(f"Готово, чат привязан к аккаунту <b>{esc(name)}</b>.\n\n" + HELP_TEXT)
        return
    if ctx.user_id is None:
        await _not_linked(ctx)
    else:
        await ctx.reply(HELP_TEXT)


HELP_TEXT = (
    "Что я умею:\n"
    "• пришлите <b>ссылку на вакансию</b> (hh.ru, Хабр Карьера или любой сайт) или <b>её текст</b> — "
    "оценю вакансию и соответствие вашему резюме, напишу сопроводительное;\n"
    "• присылаю новые подходящие вакансии из ваших поисков.\n\n"
    "/top — лучшие совпадения\n/searches — поиски и запуск\n/resumes — выбрать резюме для бота\n"
    "/notify 80 — порог уведомлений (off — выключить)\n/usage — расходы на ИИ\n/unlink — отвязать чат"
)


@command("help", "Что умеет бот", public=True)
async def cmd_help(ctx: Ctx) -> None:
    await ctx.reply(HELP_TEXT)


@command("top", "Лучшие совпадения")
async def cmd_top(ctx: Ctx) -> None:
    with session_scope() as s:
        latest = (
            select(Analysis.vacancy_id, func.max(Analysis.id).label("aid"))
            .where(Analysis.user_id == ctx.user_id, Analysis.kind == "match")
            .group_by(Analysis.vacancy_id).subquery()
        )
        rows = s.execute(
            select(Vacancy, Analysis)
            .join(latest, latest.c.vacancy_id == Vacancy.id)
            .join(Analysis, Analysis.id == latest.c.aid)
            .join(UserVacancy, (UserVacancy.vacancy_id == Vacancy.id) & (UserVacancy.user_id == ctx.user_id))
            .where(UserVacancy.status.in_(["new", "saved"]))
            .order_by(Analysis.score.desc().nullslast()).limit(8)
        ).all()
        if not rows:
            await ctx.reply("Пока нет оценённых вакансий. Запустите поиск: /searches")
            return
        lines, kb = ["<b>Лучшие совпадения</b>"], []
        for i, (v, a) in enumerate(rows, 1):
            lines.append(f"{i}. <b>{esc(int(a.score or 0))}</b> — "
                         f'<a href="{esc(web_url(f"/vacancies/{v.id}"))}">{esc(v.title)}</a>'
                         f" · {esc(v.company or '')} · {esc(salary(v))}")
            kb.append([button(f"✉️ Письмо №{i}", f"cl:{v.id}"), button(f"🙈 Скрыть №{i}", f"st:hidden:{v.id}")])
    await ctx.reply("\n".join(lines), keyboard(*kb))


@command("searches", "Мои поиски")
async def cmd_searches(ctx: Ctx) -> None:
    with session_scope() as s:
        searches = list(s.scalars(select(SavedSearch).where(SavedSearch.user_id == ctx.user_id)))
        if not searches:
            await ctx.reply("Поисков нет — создайте в веб-интерфейсе.",
                            keyboard([url_button("Открыть", web_url("/searches"))]))
            return
        kb = [[button(f"▶️ {s_.name[:40]}", f"run:{s_.id}")] for s_ in searches[:10]]
    await ctx.reply("Какой поиск запустить? Новые подходящие вакансии пришлю сюда.", keyboard(*kb))


@command("resumes", "Резюме для бота")
async def cmd_resumes(ctx: Ctx) -> None:
    with session_scope() as s:
        user = s.get(User, ctx.user_id)
        current = telegram_links.default_resume(s, user)
        resumes = list(s.scalars(select(Resume).where(Resume.user_id == ctx.user_id).order_by(Resume.id.desc())))
        if not resumes:
            await ctx.reply("Резюме нет — загрузите в веб-интерфейсе.",
                            keyboard([url_button("Открыть", web_url("/resumes"))]))
            return
        kb = [[button(("✅ " if current and r.id == current.id else "") + r.title[:50], f"res:{r.id}")]
              for r in resumes[:10]]
    await ctx.reply("По какому резюме оценивать вакансии и писать письма?", keyboard(*kb))


@command("notify", "Порог уведомлений")
async def cmd_notify(ctx: Ctx) -> None:
    arg = ctx.args.lower()
    with session_scope() as s:
        user = s.get(User, ctx.user_id)
        if arg in ("off", "выкл", "нет"):
            user.notify_min_score = 101
            msg = "Уведомления о вакансиях выключены. Включить: /notify 75"
        elif arg.isdigit() and 0 <= int(arg) <= 100:
            user.notify_min_score = int(arg)
            msg = f"Буду присылать вакансии с оценкой от {int(arg)}."
        else:
            current = user.notify_min_score or get_settings().telegram.notify_min_score
            msg = (f"Сейчас порог: {'выкл' if current > 100 else current}. "
                   "Пример: /notify 80 или /notify off")
    await ctx.reply(msg)


@command("usage", "Расходы на ИИ")
async def cmd_usage(ctx: Ctx) -> None:
    gw = get_gateway()
    spent, budget = gw.month_spent(ctx.user_id), gw.budget_for(ctx.user_id)
    limit = f" из ${budget:.2f}" if budget > 0 else " (без лимита)"
    await ctx.reply(f"В этом месяце потрачено ${spent:.2f}{limit}.")


@command("unlink", "Отвязать чат")
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


@callback("st")
async def cb_status(ctx: Ctx) -> None:
    status, _, vid = ctx.args.partition(":")
    with session_scope() as s:
        vacancy_svc.set_status(s, ctx.user_id, _int_arg(vid), status)
    labels = {"hidden": "Скрыто", "saved": "Сохранено", "applied": "Отмечено: откликнулся"}
    await ctx.api.answer_callback(ctx.callback_id, labels.get(status, "Готово"))


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
        from app.services import resumes as resume_svc

        resume = resume_svc.get_owned(s, ctx.user_id, resume_id)
        s.get(User, ctx.user_id).default_resume_id = resume.id
        title = resume.title
    await ctx.api.answer_callback(ctx.callback_id, "Выбрано")
    await ctx.reply(f"Теперь использую резюме «{esc(title)}».")


def vacancy_keyboard(vacancy_id: int) -> dict[str, Any]:
    return keyboard(
        [button("✉️ Сопроводительное", f"cl:{vacancy_id}")],
        [button("⭐ Сохранить", f"st:saved:{vacancy_id}"), button("✅ Откликнулся", f"st:applied:{vacancy_id}"),
         button("🙈 Скрыть", f"st:hidden:{vacancy_id}")],
    )


__all__ = ["COMMANDS", "CALLBACKS", "VERDICTS", "handle_update", "menu_commands", "vacancy_keyboard"]
