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
