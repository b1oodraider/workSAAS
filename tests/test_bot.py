"""Telegram bot: linking, commands, buttons, jobs and digests against a fake Bot API."""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from app.bot.api import TelegramAPI, set_api
from app.bot.digest import send_digests
from app.bot.handlers import handle_update
from app.core.db import session_scope, utcnow
from app.jobs.queue import drain
from app.llm.providers.fake import FakeProvider
from app.models import Analysis, TelegramLinkCode, User, UserVacancy
from app.services import resumes as resume_svc
from app.services import telegram_links
from app.services import vacancies as vacancy_svc

from .conftest import RESUME_TEXT, VACANCY_TEXT

CHAT = 555


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((method, params))
        result = {"username": "test_bot"} if method == "getMe" else {"message_id": 1}
        return httpx.Response(200, json={"ok": True, "result": result})

    def sent(self) -> list[dict]:
        return [p for m, p in self.calls if m == "sendMessage"]


@pytest.fixture
def tg(env):
    fake = FakeTelegram()
    api = TelegramAPI("123:TEST", transport=httpx.MockTransport(fake.handler))
    set_api(api)
    env.telegram.bot_token = "123:TEST"
    fake.api = api
    yield fake
    set_api(None)


def msg(text: str, chat_id: int = CHAT) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": chat_id, "type": "private"},
                                        "from": {"username": "alice_tg"}, "text": text}}


def cb(data: str, chat_id: int = CHAT) -> dict:
    return {"update_id": 2, "callback_query": {"id": "cq1", "data": data, "from": {},
                                               "message": {"chat": {"id": chat_id}}}}


@pytest.fixture
def linked(user_id, tg):
    with session_scope() as s:
        code = telegram_links.create_code(s, user_id)
        resume_svc.create(s, user_id, title="CV", text=RESUME_TEXT)
    return code


async def test_linking_flow(user_id, tg):
    await handle_update(tg.api, msg("/top"))
    assert "не привязан" in tg.sent()[-1]["text"]

    await handle_update(tg.api, msg("/start wrong-code"))
    assert "недействителен" in tg.sent()[-1]["text"]

    with session_scope() as s:
        code = telegram_links.create_code(s, user_id)
    await handle_update(tg.api, msg(f"/start {code}"))
    assert "привязан к аккаунту <b>alice</b>" in tg.sent()[-1]["text"]
    with session_scope() as s:
        assert s.get(User, user_id).telegram_chat_id == CHAT
        assert s.get(TelegramLinkCode, code) is None  # single use

    # expired code
    with session_scope() as s:
        code = telegram_links.create_code(s, user_id)
        s.get(TelegramLinkCode, code).expires_at = utcnow() - timedelta(minutes=1)
    await handle_update(tg.api, msg(f"/start {code}", chat_id=777))
    assert "недействителен" in tg.sent()[-1]["text"]


async def test_group_chats_are_ignored(user_id, tg):
    update = msg("/help")
    update["message"]["chat"]["type"] = "group"
    await handle_update(tg.api, update)
    assert tg.sent() == []


async def test_vacancy_text_is_analysed_and_escaped(user_id, linked, tg):
    await handle_update(tg.api, msg(f"/start {linked}"))
    FakeProvider.canned["match"] = {"summary": "Подходит <b>очень</b>", "matched": [], "gaps": [],
                                    "risks": [], "talking_points": [], "score": 88, "verdict": "strong",
                                    "recommendation": "apply"}
    await handle_update(tg.api, msg("<script>x</script> " + VACANCY_TEXT))
    assert "анализирую" in tg.sent()[-1]["text"]
    await drain()
    card = tg.sent()[-1]
    assert "88/100" in card["text"] and "&lt;b&gt;очень&lt;/b&gt;" in card["text"]
    assert "<script>" not in card["text"]
    buttons = json.loads(card["reply_markup"])["inline_keyboard"]
    assert buttons[0][0]["callback_data"].startswith("cl:")
    with session_scope() as s:
        assert s.query(UserVacancy).one().notified_at is not None  # not repeated in digest


async def test_letter_button_and_foreign_vacancy(user_id, linked, tg):
    await handle_update(tg.api, msg(f"/start {linked}"))
    with session_scope() as s:
        vid = vacancy_svc.create_manual(s, user_id, title="Python dev", text=VACANCY_TEXT).id
        other = User(username="bob", password_hash="x")
        s.add(other)
        s.flush()
        foreign = vacancy_svc.create_manual(s, other.id, title="Other", text=VACANCY_TEXT + " другое").id
    await handle_update(tg.api, cb(f"cl:{vid}"))
    await drain()
    assert "<pre>" in tg.sent()[-1]["text"]

    await handle_update(tg.api, cb(f"cl:{foreign}"))
    answers = [p for m, p in tg.calls if m == "answerCallbackQuery"]
    assert answers[-1]["text"] == "Не найдено"
    await handle_update(tg.api, cb("st:hidden:not-a-number"))
    assert [p for m, p in tg.calls if m == "answerCallbackQuery"][-1]["text"] == "Не найдено"


async def test_digest_sends_new_good_matches_once(user_id, linked, tg):
    with session_scope() as s:
        vid = vacancy_svc.create_manual(s, user_id, title="Python dev", text=VACANCY_TEXT).id
    await handle_update(tg.api, msg(f"/start {linked}"))  # linking marks old vacancies as notified
    with session_scope() as s:
        s.query(UserVacancy).update({"notified_at": None})
        s.add(Analysis(user_id=user_id, kind="match", resume_id=None, vacancy_id=vid,
                       output={"summary": "ok"}, score=90, provider="fake", model="m", prompt_version="2"))
    assert await send_digests(tg.api) == 1
    assert "Новые подходящие вакансии: 1" in tg.sent()[-1]["text"]
    assert await send_digests(tg.api) == 0

    await handle_update(tg.api, msg("/notify off"))
    with session_scope() as s:
        assert s.get(User, user_id).notify_min_score == 101


async def test_top_and_usage_commands(user_id, linked, tg):
    await handle_update(tg.api, msg(f"/start {linked}"))
    await handle_update(tg.api, msg("/top"))
    assert "Пока нет оценённых" in tg.sent()[-1]["text"]
    await handle_update(tg.api, msg("/usage"))
    assert "потрачено $0.00" in tg.sent()[-1]["text"]
    await handle_update(tg.api, msg("/resumes"))
    assert json.loads(tg.sent()[-1]["reply_markup"])["inline_keyboard"][0][0]["text"] == "✅ CV"


async def test_tracker_reminder_and_follow_up(user_id, linked, tg):
    from app.bot.digest import send_reminders

    await handle_update(tg.api, msg(f"/start {linked}"))
    with session_scope() as s:
        vid = vacancy_svc.create_manual(s, user_id, title="Python dev", text=VACANCY_TEXT).id
        vacancy_svc.set_status(s, user_id, vid, "applied")
        _, uv = vacancy_svc.get_for_user(s, user_id, vid)
        uv.next_action_at = utcnow() - timedelta(minutes=1)
    assert await send_reminders(tg.api) == 1
    assert "Напомнить о себе" in tg.sent()[-1]["text"]
    assert await send_reminders(tg.api) == 0

    await handle_update(tg.api, cb(f"sn:{vid}"))
    with session_scope() as s:
        _, uv = vacancy_svc.get_for_user(s, user_id, vid)
        assert uv.next_action_at > utcnow() and uv.reminded_at is None

    FakeProvider.canned["follow_up"] = {"subject": "Напоминаю о себе", "body": "Добрый день!",
                                        "when_to_send": "утром", "tips": []}
    await handle_update(tg.api, cb(f"fu:{vid}"))
    await drain()
    assert "Напоминаю о себе" in tg.sent()[-1]["text"]


async def test_bot_token_never_reaches_logs(tg, caplog):
    import logging

    from app.bot.api import RedactTokenFilter, silence_http_logs

    silence_http_logs()
    assert logging.getLogger("httpx").level == logging.WARNING
    record = logging.LogRecord("x", logging.INFO, "", 0,
                               "POST https://api.telegram.org/bot7701159678:AAAAAAAAAAAAAAAAAAAAAAAAAA/getUpdates",
                               (), None)
    RedactTokenFilter().filter(record)
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAA" not in record.getMessage() and "bot<redacted>" in record.getMessage()


class FlakyTelegram(FakeTelegram):
    def __init__(self, error_code: int | None) -> None:
        super().__init__()
        self.error_code = error_code

    def handler(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "sendMessage" and self.error_code:
            self.calls.append((method, {}))
            return httpx.Response(self.error_code, json={"ok": False, "error_code": self.error_code,
                                                         "description": "boom"})
        return super().handler(request)


def _good_match(user_id, vid):
    with session_scope() as s:
        s.query(UserVacancy).update({"notified_at": None})
        s.add(Analysis(user_id=user_id, kind="match", vacancy_id=vid, output={"summary": "&" * 1000},
                       score=95, provider="fake", model="m", prompt_version="2"))


@pytest.mark.parametrize("code,expect_notified,expect_linked", [
    (500, False, True),   # transient: claim released, retried next tick
    (400, True, True),    # bad request: claim kept, no infinite retry
    (403, True, False),   # bot blocked by user: chat unlinked
])
async def test_digest_error_handling(user_id, env, code, expect_notified, expect_linked):
    fake = FlakyTelegram(code)
    api = TelegramAPI("123:TEST", transport=httpx.MockTransport(fake.handler))
    with session_scope() as s:
        s.get(User, user_id).telegram_chat_id = CHAT
        vid = vacancy_svc.create_manual(s, user_id, title="T" * 500, text=VACANCY_TEXT).id
    _good_match(user_id, vid)
    assert await send_digests(api) == 0
    with session_scope() as s:
        assert (s.query(UserVacancy).one().notified_at is not None) == expect_notified
        assert (s.get(User, user_id).telegram_chat_id is not None) == expect_linked


async def test_digest_fits_telegram_limit_and_is_not_sent_twice(user_id, tg):
    from app.bot.api import MAX_TEXT

    with session_scope() as s:
        s.get(User, user_id).telegram_chat_id = CHAT
        ids = [vacancy_svc.create_manual(s, user_id, title=f"Вакансия {i} " + "Ж" * 300,
                                         text=VACANCY_TEXT + str(i)).id for i in range(10)]
    with session_scope() as s:
        for vid in ids:
            s.add(Analysis(user_id=user_id, kind="match", vacancy_id=vid, output={"summary": "<&>" * 200},
                           score=90, provider="fake", model="m", prompt_version="2"))
    assert await send_digests(tg.api) == 1
    text = tg.sent()[-1]["text"]
    assert len(text) <= MAX_TEXT and tg.sent()[-1].get("parse_mode") == "HTML"
    assert "&lt;&amp;&gt;" in text
    # the rest arrives on the next ticks, each message within the limit, nothing repeated
    seen = set()
    for _ in range(10):
        if not await send_digests(tg.api):
            break
        assert len(tg.sent()[-1]["text"]) <= MAX_TEXT
    for p in tg.sent():
        for row in json.loads(p["reply_markup"])["inline_keyboard"]:
            data = row[0]["callback_data"]
            assert data not in seen
            seen.add(data)
    with session_scope() as s:
        assert s.query(UserVacancy).filter(UserVacancy.notified_at.is_(None)).count() == 0
    assert len(seen) == 10


def test_local_time_conversion(env):
    from datetime import datetime

    from app.core.db import from_local, to_local

    env.timezone = "Europe/Moscow"
    utc = from_local(datetime(2026, 9, 29, 12, 0))
    assert utc == datetime(2026, 9, 29, 9, 0) and to_local(utc) == datetime(2026, 9, 29, 12, 0)
