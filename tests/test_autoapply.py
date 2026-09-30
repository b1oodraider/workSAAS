"""Auto-apply pipeline with a fake applier: selection, limits, modes, kill switch."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.apply import registry as apply_registry
from app.apply.base import Applier, ApplyResult
from app.core.config import ModelPrice
from app.core.db import from_local, session_scope, to_local, utcnow
from app.core.errors import NotFound
from app.jobs.queue import drain
from app.llm.providers.fake import FakeProvider
from app.models import Analysis, Application, ApplicationStatus, User, UserVacancy
from app.services import autoapply
from app.services import resumes as resume_svc
from app.services import vacancies as vacancy_svc
from app.sources.base import VacancyDraft

from .conftest import RESUME_TEXT, VACANCY_TEXT

LETTER = {"subject": "Отклик", "body": "Здравствуйте! Мой опыт с FastAPI…", "key_points_used": [], "warnings": []}


class FakeApplier(Applier):
    source = "hh"
    title = "hh.ru"

    def __init__(self, results=None):
        self.results = list(results or [])
        self.requests = []

    def is_ready(self, user_id):
        return True, ""

    async def apply(self, req):
        self.requests.append(req)
        return self.results.pop(0) if self.results else ApplyResult("applied", "ok")


@pytest.fixture
def applier(monkeypatch):
    fake = FakeApplier()
    monkeypatch.setitem(apply_registry.APPLIERS, "hh", fake)
    FakeProvider.canned["cover_letter"] = LETTER
    return fake


@pytest.fixture
def setup(user_id):
    """A resume and five hh vacancies with match scores 95, 90, 85, 75, 60."""
    with session_scope() as s:
        rid = resume_svc.create(s, user_id, title="CV", text=RESUME_TEXT).id
        vids = []
        for i, score in enumerate([95, 90, 85, 75, 60]):
            v, _ = vacancy_svc.upsert(s, VacancyDraft(source="hh", external_id=str(100 + i), title=f"Python {i}",
                                                      company=f"Co{i}", description=VACANCY_TEXT))
            vacancy_svc.attach(s, user_id, v.id)
            s.add(Analysis(user_id=user_id, kind="match", resume_id=rid, vacancy_id=v.id, score=score,
                           output={"summary": "s", "recommendation": "apply"}, provider="f", model="m",
                           prompt_version="2"))
            vids.append(v.id)
    return {"resume": rid, "vacancies": vids}


def enable(user_id, **overrides):
    params = dict(enabled=True, mode="auto", min_score=80, daily_limit=10, min_interval_s=60,
                  active_from_hour=0, active_to_hour=24, resume_id=None, hh_resume_title="",
                  letter_tone="friendly")
    params.update(overrides)
    with session_scope() as s:
        autoapply.update_config(s, user_id, **params)


def statuses(user_id):
    with session_scope() as s:
        return [a.status.value for a in s.scalars(select(Application).where(Application.user_id == user_id)
                                                  .order_by(Application.score.desc()))]


def test_plan_selects_best_matches_only(user_id, setup, applier):
    enable(user_id, daily_limit=2)
    with session_scope() as s:
        ids = autoapply.plan(s, user_id)
        scores = [s.get(Application, i).score for i in ids]
        assert scores == [95, 90]            # daily limit 2, best first
        assert autoapply.plan(s, user_id) == []  # no duplicates, no room left


def test_plan_skips_red_flags_skip_recommendation_and_other_sources(user_id, setup, applier):
    v95, v90, v85 = setup["vacancies"][:3]
    with session_scope() as s:
        s.add(Analysis(user_id=user_id, kind="vacancy_review", vacancy_id=v95, score=30,
                       output={"red_flags": [{"severity": "high", "text": "серая зп", "evidence": "…"}]},
                       provider="f", model="m", prompt_version="2"))
        match90 = s.scalar(select(Analysis).where(Analysis.vacancy_id == v90, Analysis.kind == "match"))
        match90.output = {"summary": "s", "recommendation": "skip"}
        v, _ = vacancy_svc.upsert(s, VacancyDraft(source="habr", external_id="1", title="Habr job",
                                                  company="H", description=VACANCY_TEXT))
        vacancy_svc.attach(s, user_id, v.id)
        s.add(Analysis(user_id=user_id, kind="match", resume_id=setup["resume"], vacancy_id=v.id, score=99,
                       output={"recommendation": "apply"}, provider="f", model="m", prompt_version="2"))
    enable(user_id)
    with session_scope() as s:
        picked = [s.get(Application, i).vacancy_id for i in autoapply.plan(s, user_id)]
    assert picked == [v85]  # 95 has red flags, 90 is "skip", habr has no applier, 75/60 < 80


async def test_sends_one_at_a_time_with_interval_and_marks_applied(user_id, setup, applier):
    enable(user_id)
    started = autoapply.autoapply_tick()
    assert len(started) == 1
    assert autoapply.autoapply_tick() == []  # one is "sending": nothing else starts
    await drain()
    assert statuses(user_id)[0] == "applied"
    req = applier.requests[0]
    assert req.external_id == "100" and req.letter.startswith("Здравствуйте")
    with session_scope() as s:
        uv = s.scalars(select(UserVacancy).where(UserVacancy.vacancy_id == setup["vacancies"][0])).one()
        assert uv.status.value == "applied" and uv.next_action_at is not None  # tracker follow-up
        # interval: nothing may go out right after, but it can a few minutes later
        assert autoapply.next_to_send(s, user_id) is None
        assert autoapply.next_to_send(s, user_id, now=utcnow() + timedelta(minutes=5)) is not None


def test_active_hours_and_daily_limit(user_id, setup, applier):
    enable(user_id, active_from_hour=9, active_to_hour=21, daily_limit=1)
    with session_scope() as s:
        autoapply.plan(s, user_id)
        today = to_local(utcnow()).replace(minute=0, second=0, microsecond=0)
        night, day = from_local(today.replace(hour=3)), from_local(today.replace(hour=12))
        assert autoapply.next_to_send(s, user_id, now=night) is None
        app = autoapply.next_to_send(s, user_id, now=day)
        assert app is not None
        app.status, app.sent_at = ApplicationStatus.applied, day
        assert autoapply.next_to_send(s, user_id, now=day + timedelta(hours=1)) is None  # limit 1/day


def test_config_is_clamped_to_hard_limits(user_id, env):
    enable(user_id, daily_limit=1000, min_interval_s=1, min_score=10)
    with session_scope() as s:
        cfg = autoapply.get_config(s, user_id)
        assert cfg.daily_limit == env.apply.hard_daily_max
        assert cfg.min_interval_s == env.apply.min_interval_floor_s
        assert cfg.min_score == 50


async def test_confirm_mode_waits_for_approval(user_id, setup, applier):
    enable(user_id, mode="confirm")
    assert autoapply.autoapply_tick() == []
    assert set(statuses(user_id)) == {"queued"}
    with session_scope() as s:
        first, second = s.scalars(select(Application).order_by(Application.score.desc())).all()[:2]
        autoapply.approve(s, user_id, first.id)
        autoapply.cancel(s, user_id, second.id)
    assert len(autoapply.autoapply_tick()) == 1
    await drain()
    assert statuses(user_id)[:2] == ["applied", "cancelled"]


async def test_blocked_pauses_and_requeues(user_id, setup, applier):
    applier.results = [ApplyResult("blocked", "hh.ru показал капчу")]
    enable(user_id)
    autoapply.autoapply_tick()
    await drain()
    with session_scope() as s:
        cfg = autoapply.get_config(s, user_id)
        assert "капчу" in cfg.paused_reason and not autoapply.is_running(cfg)
        app = s.scalars(select(Application).order_by(Application.score.desc())).first()
        assert app.status == ApplicationStatus.queued and app.sent_at is None
    assert autoapply.autoapply_tick() == []  # paused: nothing starts
    with session_scope() as s:
        autoapply.resume_after_pause(s, user_id)
    assert len(autoapply.autoapply_tick()) == 1


async def test_consecutive_failures_pause(user_id, setup, applier, env, monkeypatch):
    applier.results = [ApplyResult("failed", "нет кнопки")] * 3
    enable(user_id, min_interval_s=60)
    for _ in range(3):
        with session_scope() as s:  # skip the interval between attempts
            for a in s.scalars(select(Application)):
                if a.sent_at:
                    a.sent_at -= timedelta(hours=1)
        autoapply.autoapply_tick()
        await drain()
    with session_scope() as s:
        assert "ошибки подряд" in autoapply.get_config(s, user_id).paused_reason
    assert statuses(user_id).count("failed") == 3


async def test_budget_exceeded_pauses_before_sending(user_id, setup, applier, env):
    FakeProvider.canned.pop("cover_letter")
    env.llm.prices["fake-model"] = ModelPrice(input=1_000_000.0, output=0.0)
    with session_scope() as s:
        s.get(User, user_id).monthly_budget_usd = 0.01
        s.add(Analysis(user_id=user_id, kind="noop", output={}, provider="f", model="m", prompt_version="1"))
    from app.models import LLMUsage

    with session_scope() as s:
        s.add(LLMUsage(user_id=user_id, task="x", provider="p", model="m", cost_usd=1.0))
    enable(user_id)
    autoapply.autoapply_tick()
    await drain()
    assert applier.requests == []
    with session_scope() as s:
        assert "бюджет" in autoapply.get_config(s, user_id).paused_reason.lower()


def test_cannot_touch_other_users_applications(user_id, setup, applier):
    enable(user_id)
    with session_scope() as s:
        app_id = autoapply.plan(s, user_id)[0]
        other = User(username="bob", password_hash="x")
        s.add(other)
        s.flush()
        with pytest.raises(NotFound):
            autoapply.approve(s, other.id, app_id)
        with pytest.raises(NotFound):
            autoapply.cancel(s, other.id, app_id)


def test_global_switch_disables_everything(user_id, setup, applier, env):
    enable(user_id)
    env.apply.enabled = False
    assert autoapply.autoapply_tick() == []


def test_web_page_and_actions(user_id, setup, applier, env, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    env.data_dir = str(tmp_path)
    with TestClient(create_app(start_background=False)) as client:
        client.post("/login", data={"username": "alice", "password": "password123"})
        r = client.get("/autoapply")
        assert r.status_code == 200 and "Выключены" in r.text
        r = client.post("/autoapply/settings", data={"enabled": "true", "mode": "confirm", "min_score": "80",
                                                      "daily_limit": "5", "min_interval_min": "3",
                                                      "active_from_hour": "0", "active_to_hour": "24",
                                                      "letter_tone": "formal"})
        assert "автоотклики включены" in r.text
        r = client.post("/autoapply/plan")
        assert "В очередь добавлено: 3" in r.text
        with session_scope() as s:
            first = s.scalars(select(Application).order_by(Application.score.desc())).first()
        client.post(f"/autoapply/{first.id}/approve")
        with session_scope() as s:
            assert s.get(Application, first.id).status == ApplicationStatus.approved
        bad = client.post("/autoapply/session", files={"file": ("s.json", b'{"cookies": []}', "application/json")})
        assert "нет cookies" in bad.text
        ok = client.post("/autoapply/session", files={"file": ("s.json",
                         b'{"cookies": [{"name": "hhtoken", "value": "1", "domain": ".hh.ru"}]}', "application/json")})
        assert "Сессия hh.ru сохранена" in ok.text
        assert client.post("/autoapply/pause").status_code == 200
        with session_scope() as s:
            assert autoapply.get_config(s, user_id).paused_reason
        client.post("/logout")
        with session_scope() as s:
            from app.core.security import hash_password

            s.add(User(username="bob", password_hash=hash_password("password123")))
        client.post("/login", data={"username": "bob", "password": "password123"})
        assert client.post(f"/autoapply/{first.id}/cancel").status_code == 404


async def test_bot_confirm_flow_and_reports(user_id, setup, applier, env):
    import json

    import httpx

    from app.bot.api import TelegramAPI
    from app.bot.digest import send_autoapply_updates
    from app.bot.handlers import handle_update

    sent = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        from urllib.parse import parse_qs

        sent.append((method, {k: v[0] for k, v in parse_qs(request.content.decode()).items()}))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    api = TelegramAPI("1:X", transport=httpx.MockTransport(handler))
    with session_scope() as s:
        s.get(User, user_id).telegram_chat_id = 555
    enable(user_id, mode="confirm")
    autoapply.autoapply_tick()  # plans the queue, sends nothing in confirm mode
    assert await send_autoapply_updates(api) == 1
    msg = [p for m, p in sent if m == "sendMessage"][-1]
    buttons = json.loads(msg["reply_markup"])["inline_keyboard"]
    approve_data = buttons[0][0]["callback_data"]
    assert approve_data.startswith("aa:") and "Откликнуться" in msg["text"]
    assert await send_autoapply_updates(api) == 0  # not asked twice

    await handle_update(api, {"update_id": 1, "callback_query": {
        "id": "c", "data": approve_data, "from": {}, "message": {"chat": {"id": 555}}}})
    assert len(autoapply.autoapply_tick()) == 1
    await drain()
    assert await send_autoapply_updates(api) == 1
    assert "✅" in [p for m, p in sent if m == "sendMessage"][-1]["text"]

    applier.results = [ApplyResult("blocked", "hh.ru показал капчу")]
    with session_scope() as s:
        second = s.scalars(select(Application).where(Application.status == ApplicationStatus.queued)
                           .order_by(Application.score.desc())).first()
        autoapply.approve(s, user_id, second.id)
        for a in s.scalars(select(Application)):
            if a.sent_at:
                a.sent_at -= timedelta(hours=1)
    autoapply.autoapply_tick()
    await drain()
    await send_autoapply_updates(api)
    assert any("на паузе" in p.get("text", "") for m, p in sent if m == "sendMessage")
