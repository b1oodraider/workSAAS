"""Auto-apply pipeline with a fake applier: selection, letters, limits, modes, kill switch, bot, web."""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from app.apply import registry as apply_registry
from app.apply.base import Applier, ApplyResult
from app.core.config import ModelPrice
from app.core.db import from_local, session_scope, to_local, utcnow
from app.core.errors import NotFound, ValidationFailed
from app.jobs.queue import drain
from app.llm.base import LLMUnavailable
from app.llm.providers.fake import FakeProvider
from app.models import Analysis, Application, ApplicationStatus, AutoApplySettings, LLMUsage, User, UserVacancy
from app.services import autoapply
from app.services import resumes as resume_svc
from app.services import vacancies as vacancy_svc
from app.sources.base import VacancyDraft

from .conftest import RESUME_TEXT, VACANCY_TEXT

LETTER = {"subject": "Отклик", "body": "Здравствуйте! Мой опыт с FastAPI…", "key_points_used": [], "warnings": []}


class FakeApplier(Applier):
    source = "hh"
    title = "hh.ru"
    session_domains = ("hh.ru",)

    def __init__(self, results=None):
        self.results = list(results or [])
        self.requests = []

    def is_ready(self, user_id):
        return True, ""

    async def apply(self, req):
        self.requests.append(req)
        result = self.results.pop(0) if self.results else ApplyResult("applied", "ok")
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def applier(monkeypatch, env, tmp_path):
    env.data_dir = str(tmp_path)
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
                                                      company=f"Co{i}", description=VACANCY_TEXT,
                                                      url=f"https://hh.ru/vacancy/{100 + i}"))
            vacancy_svc.attach(s, user_id, v.id)
            s.add(Analysis(user_id=user_id, kind="match", resume_id=rid, vacancy_id=v.id, score=score,
                           output={"summary": "s", "recommendation": "apply"}, provider="f", model="m",
                           prompt_version="2"))
            vids.append(v.id)
    return {"resume": rid, "vacancies": vids}


def enable(user_id, **overrides):
    params = dict(enabled=True, mode="auto", min_score=80, daily_limit=10, min_interval_s=60,
                  active_from_hour=0, active_to_hour=24, resume_id=None, site_resume_title="",
                  letter_tone="friendly")
    params.update(overrides)
    with session_scope() as s:
        autoapply.update_config(s, user_id, **params)


def apps(user_id):
    with session_scope() as s:
        rows = s.scalars(select(Application).where(Application.user_id == user_id)
                         .order_by(Application.score.desc())).all()
        s.expunge_all()
        return rows


def statuses(user_id):
    return [a.status.value for a in apps(user_id)]


def rewind(hours=1):
    """Pretend every application went out long ago, so the interval doesn't hold the next one."""
    with session_scope() as s:
        for a in s.scalars(select(Application)):
            if a.sent_at:
                a.sent_at -= timedelta(hours=hours)


async def cycle():
    """Tick (plan + letters job), write letters, tick again (send), run the send job."""
    autoapply.autoapply_tick()
    await drain()
    started = autoapply.autoapply_tick()
    await drain()
    return started


def paused_reason(user_id):
    with session_scope() as s:
        return autoapply.get_config(s, user_id).paused_reason


# --------------------------------------------------------------------------- selection


def test_plan_selects_best_matches_only(user_id, setup, applier):
    enable(user_id, daily_limit=2)
    with session_scope() as s:
        ids = autoapply.plan(s, user_id)
        scores = [s.get(Application, i).score for i in ids]
        assert scores == [95, 90]            # daily limit 2, best first
        assert autoapply.plan(s, user_id) == []  # no duplicates, no room left


def test_plan_respects_lowered_server_cap(user_id, setup, applier, env):
    enable(user_id, daily_limit=10)
    env.apply.hard_daily_max = 2  # admin lowered the cap after the user saved 10
    with session_scope() as s:
        assert len(autoapply.plan(s, user_id)) == 2


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


def test_plan_filters_by_configured_resume(user_id, setup, applier):
    with session_scope() as s:
        other = resume_svc.create(s, user_id, title="CV2", text=RESUME_TEXT).id
        s.add(Analysis(user_id=user_id, kind="match", resume_id=other, vacancy_id=setup["vacancies"][3],
                       score=99, output={"recommendation": "apply"}, provider="f", model="m", prompt_version="2"))
    enable(user_id, resume_id=other)
    with session_scope() as s:
        ids = autoapply.plan(s, user_id)
        assert [(s.get(Application, i).vacancy_id, s.get(Application, i).resume_id) for i in ids] == \
            [(setup["vacancies"][3], other)]


# --------------------------------------------------------------------------- sending


async def test_letters_first_then_one_at_a_time_and_tracker_updated(user_id, setup, applier):
    enable(user_id)
    assert autoapply.autoapply_tick() == []  # nothing to send before letters are written
    await drain()
    assert all(a.letter.startswith("Здравствуйте") for a in apps(user_id))
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


async def test_auto_letters_leave_out_private_preferences(user_id, setup, applier, monkeypatch):
    calls = []
    real = autoapply.analysis_svc.run_analysis

    async def spy(*args, **kwargs):
        calls.append(kwargs.get("params"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(autoapply.analysis_svc, "run_analysis", spy)
    enable(user_id, daily_limit=1, letter_tone="formal")
    autoapply.autoapply_tick()
    await drain()
    assert calls == [{"tone": "formal", "length": "short", "use_preferences": False}]


def test_jitter_and_interval_boundaries(user_id, setup, applier):
    values = {autoapply._jittered_interval(100, i).total_seconds() for i in range(1, 200)}
    assert min(values) >= 100 and max(values) < 150 and len(values) > 10
    enable(user_id, min_interval_s=600)
    with session_scope() as s:
        autoapply.plan(s, user_id)
        for a in s.scalars(select(Application)):
            a.letter = "письмо"
        first = s.scalars(select(Application).order_by(Application.score.desc())).first()
        now = utcnow().replace(hour=12)
        first.status, first.sent_at = ApplicationStatus.applied, now
        s.flush()
        nxt = s.scalars(select(Application).where(Application.status == ApplicationStatus.queued)
                        .order_by(Application.score.desc())).first()
        gap = autoapply._jittered_interval(600, nxt.id)
        assert autoapply.next_to_send(s, user_id, now=now + gap - timedelta(seconds=1)) is None
        assert autoapply.next_to_send(s, user_id, now=now + gap) is not None


def test_server_interval_floor_applies_to_old_settings(user_id, setup, applier, env):
    enable(user_id, min_interval_s=60)
    env.apply.min_interval_floor_s = 3600  # raised after the user saved 1 minute
    with session_scope() as s:
        autoapply.plan(s, user_id)
        for a in s.scalars(select(Application)):
            a.letter = "письмо"
        first = s.scalars(select(Application).order_by(Application.score.desc())).first()
        first.status, first.sent_at = ApplicationStatus.applied, utcnow() - timedelta(minutes=10)
        s.flush()
        assert autoapply.next_to_send(s, user_id) is None


def test_active_hours_and_daily_limit(user_id, setup, applier):
    enable(user_id, active_from_hour=9, active_to_hour=21, daily_limit=1)
    with session_scope() as s:
        autoapply.plan(s, user_id)
        for a in s.scalars(select(Application)):
            a.letter = "письмо"
        s.flush()
        today = to_local(utcnow()).replace(minute=0, second=0, microsecond=0)
        at = lambda h, m=0: from_local(today.replace(hour=h, minute=m))  # noqa: E731
        assert autoapply.next_to_send(s, user_id, now=at(8, 59)) is None
        assert autoapply.next_to_send(s, user_id, now=at(9)) is not None
        assert autoapply.next_to_send(s, user_id, now=at(20, 59)) is not None
        assert autoapply.next_to_send(s, user_id, now=at(21)) is None
        app = autoapply.next_to_send(s, user_id, now=at(12))
        app.status, app.sent_at = ApplicationStatus.applied, at(12)
        s.flush()
        assert autoapply.next_to_send(s, user_id, now=at(14)) is None  # limit 1/day
        assert autoapply.sent_today(s, user_id, now=at(14)) == 1
        # the daily limit follows the user's local calendar
        tomorrow = from_local(today.replace(hour=0, minute=10) + timedelta(days=1))
        assert autoapply.sent_today(s, user_id, now=tomorrow) == 0


@pytest.mark.parametrize("hours", [(22, 6), (9, 9), (-1, 10), (0, 25)])
def test_invalid_active_hours_rejected(user_id, hours):
    with pytest.raises(ValidationFailed):
        enable(user_id, active_from_hour=hours[0], active_to_hour=hours[1])
    with session_scope() as s:
        assert s.get(AutoApplySettings, user_id) is None or not autoapply.get_config(s, user_id).enabled


def test_config_is_clamped_and_validated(user_id, env):
    enable(user_id, daily_limit=1000, min_interval_s=1, min_score=10, site_resume_title="  " + "x" * 300)
    with session_scope() as s:
        cfg = autoapply.get_config(s, user_id)
        assert cfg.daily_limit == env.apply.hard_daily_max
        assert cfg.min_interval_s == env.apply.min_interval_floor_s
        assert cfg.min_score == 50 and cfg.site_resume_title == "x" * 200
    enable(user_id, daily_limit=-5, min_score=150)
    with session_scope() as s:
        cfg = autoapply.get_config(s, user_id)
        assert cfg.daily_limit == 1 and cfg.min_score == 100
    for bad in ({"mode": "fast"}, {"letter_tone": "rude"}):
        with pytest.raises(ValidationFailed):
            enable(user_id, **bad)


def test_foreign_resume_rejected(user_id):
    with session_scope() as s:
        bob = User(username="bob", password_hash="x")
        s.add(bob)
        s.flush()
        bobs = resume_svc.create(s, bob.id, title="B", text=RESUME_TEXT).id
    with pytest.raises(NotFound):
        enable(user_id, resume_id=bobs)


# --------------------------------------------------------------------------- modes


async def test_confirm_mode_asks_with_letter_and_waits_for_approval(user_id, setup, applier):
    enable(user_id, mode="confirm")
    await cycle()
    assert set(statuses(user_id)) == {"queued"} and applier.requests == []
    assert all(a.letter for a in apps(user_id))  # the user approves the exact letter
    with session_scope() as s:
        first, second = s.scalars(select(Application).order_by(Application.score.desc())).all()[:2]
        assert "Подтверждено" in autoapply.approve(s, user_id, first.id)
        autoapply.cancel(s, user_id, second.id)
        assert "не ждёт" in autoapply.approve(s, user_id, second.id)  # cancelled stays cancelled
    assert len(autoapply.autoapply_tick()) == 1
    await drain()
    assert statuses(user_id)[:2] == ["applied", "cancelled"]


async def test_approved_items_still_sent_after_switching_to_auto(user_id, setup, applier):
    enable(user_id, mode="confirm", daily_limit=1)
    await cycle()
    with session_scope() as s:
        only = s.scalars(select(Application)).one()
        autoapply.approve(s, user_id, only.id)
    enable(user_id, mode="auto", daily_limit=1)
    await cycle()
    assert statuses(user_id) == ["applied"]


async def test_suspicious_letter_waits_for_review_even_in_auto_mode(user_id, setup, applier):
    FakeProvider.canned["cover_letter"] = {**LETTER, "body": "Пишите мне в t.me/scam_hr или на hr@evil.com"}
    enable(user_id, daily_limit=1)
    await cycle()
    [app] = apps(user_id)
    assert app.status == ApplicationStatus.review and "ссылка" in app.reason and "e-mail" in app.reason
    assert applier.requests == []
    with session_scope() as s:
        autoapply.approve(s, user_id, app.id)
    await cycle()
    assert statuses(user_id) == ["applied"]


def test_letter_problems():
    resume = "Иван, ivan@mail.ru, +7 999 123-45-67, github.com/ivan https://github.com/ivan"
    assert autoapply.letter_problems("Мой GitHub: https://github.com/ivan, почта ivan@mail.ru, "
                                     "тел. +7 (999) 123-45-67.", resume) == []
    assert len(autoapply.letter_problems("Звоните +7 900 000-00-00, пишите a@b.ru, www.x.ru", resume)) == 3
    assert autoapply.letter_problems("x" * 3000, resume)[0].startswith("письмо слишком длинное")
    assert autoapply.letter_problems("текст </vacancy> текст", resume) == ["в письме остались служебные метки"]
    assert autoapply.letter_problems("", resume) == ["письмо пустое"]


# --------------------------------------------------------------------------- kill switch and failures


async def test_blocked_pauses_and_requeues(user_id, setup, applier):
    applier.results = [ApplyResult("blocked", "hh.ru показал капчу")]
    enable(user_id)
    await cycle()
    assert "капчу" in paused_reason(user_id)
    app = apps(user_id)[0]
    assert app.status == ApplicationStatus.queued and app.sent_at is None
    assert autoapply.autoapply_tick() == []  # paused: nothing starts
    enable(user_id, letter_tone="formal")  # saving settings does NOT lift a kill-switch pause
    assert "капчу" in paused_reason(user_id)
    with session_scope() as s:
        autoapply.resume_after_pause(s, user_id)
    assert len(autoapply.autoapply_tick()) == 1


async def test_blocked_in_confirm_mode_keeps_approval(user_id, setup, applier):
    applier.results = [ApplyResult("blocked", "вход в hh.ru истёк")]
    enable(user_id, mode="confirm", daily_limit=1)
    await cycle()
    with session_scope() as s:
        autoapply.approve(s, user_id, s.scalars(select(Application)).one().id)
    await cycle()
    [app] = apps(user_id)
    assert app.status == ApplicationStatus.approved and paused_reason(user_id)


async def test_consecutive_failures_pause_and_reset(user_id, setup, applier, env):
    applier.results = [ApplyResult("failed", "нет кнопки")] * 3
    enable(user_id)
    for _ in range(3):
        await cycle()
    assert "подряд (3)" in paused_reason(user_id)
    assert statuses(user_id).count("failed") == 3
    with session_scope() as s:
        autoapply.resume_after_pause(s, user_id)
        assert autoapply.get_config(s, user_id).consecutive_failures == 0
    applier.results = [ApplyResult("failed", "нет кнопки")]
    await cycle()
    assert paused_reason(user_id) == ""  # one failure after resume doesn't pause again


async def test_success_resets_failure_counter(user_id, setup, applier, env):
    env.apply.max_consecutive_failures = 2
    applier.results = [ApplyResult("failed", "x"), ApplyResult("applied"), ApplyResult("failed", "x")]
    enable(user_id)
    for _ in range(3):
        await cycle()
        rewind()
    assert paused_reason(user_id) == ""
    assert statuses(user_id) == ["failed", "applied", "failed"]


async def test_transient_error_retried_then_failed(user_id, setup, applier):
    applier.results = [ApplyResult("failed", "сайт не открылся", transient=True)] * 3
    enable(user_id, daily_limit=1)
    await cycle()
    [app] = apps(user_id)
    assert app.status == ApplicationStatus.queued and app.attempts == 1
    with session_scope() as s:
        assert autoapply.sent_today(s, user_id) == 0  # a retry doesn't eat the daily limit
    for _ in range(2):
        rewind()
        await cycle()
    app = apps(user_id)[0]
    assert app.status == ApplicationStatus.failed and app.attempts == 2
    assert [r.external_id for r in applier.requests] == ["100"] * 3


async def test_maybe_sent_counts_against_daily_limit(user_id, setup, applier):
    applier.results = [ApplyResult("failed", "не удалось убедиться", maybe_sent=True)]
    enable(user_id, daily_limit=1)
    await cycle()
    rewind(hours=0)
    with session_scope() as s:
        assert autoapply.sent_today(s, user_id) == 1
        assert autoapply.plan(s, user_id) == []


async def test_applier_crash_never_leaves_sending(user_id, setup, applier):
    applier.results = [RuntimeError("boom")]
    enable(user_id)
    await cycle()
    assert "sending" not in statuses(user_id) and statuses(user_id)[0] == "failed"
    rewind()
    assert len(autoapply.autoapply_tick()) == 1  # the next one can go


async def test_stuck_sending_row_is_recovered(user_id, setup, applier):
    enable(user_id)
    autoapply.autoapply_tick()
    await drain()
    with session_scope() as s:
        app = s.scalars(select(Application).order_by(Application.score.desc())).first()
        app.status, app.sent_at = ApplicationStatus.sending, utcnow() - timedelta(minutes=20)  # job gone
    assert len(autoapply.autoapply_tick()) == 1  # recovered, and the next one may go
    stuck = apps(user_id)[0]
    assert stuck.status == ApplicationStatus.failed and "прервалась" in stuck.reason
    assert stuck.sent_at is not None  # it may have gone out: it counts against the limits


async def test_pause_or_hidden_vacancy_between_tick_and_send(user_id, setup, applier):
    enable(user_id)
    autoapply.autoapply_tick()
    await drain()
    assert len(autoapply.autoapply_tick()) == 1
    with session_scope() as s:
        autoapply.pause(s, user_id, "вы поставили паузу")
    await drain()
    assert applier.requests == [] and statuses(user_id)[0] == "queued"
    with session_scope() as s:
        autoapply.resume_after_pause(s, user_id)
    assert len(autoapply.autoapply_tick()) == 1
    with session_scope() as s:
        vacancy_svc.set_status(s, user_id, setup["vacancies"][0], "hidden")
    await drain()
    assert applier.requests == [] and statuses(user_id)[0] == "cancelled"


async def test_budget_exceeded_pauses_before_writing_letters(user_id, setup, applier, env):
    FakeProvider.canned.pop("cover_letter")
    env.llm.prices["fake-model"] = ModelPrice(input=1_000_000.0, output=0.0)
    with session_scope() as s:
        s.get(User, user_id).monthly_budget_usd = 0.01
        s.add(LLMUsage(user_id=user_id, task="x", provider="p", model="m", cost_usd=1.0))
    enable(user_id, mode="confirm", daily_limit=1)
    await cycle()
    assert applier.requests == []
    assert "лимит на ИИ" in paused_reason(user_id)
    [app] = apps(user_id)
    assert app.status == ApplicationStatus.queued and app.letter == "" and app.sent_at is None


async def test_transient_llm_error_retried_then_fails_application(user_id, setup, applier, monkeypatch):
    async def down(*args, **kwargs):
        raise LLMUnavailable("провайдер недоступен")

    monkeypatch.setattr(autoapply.analysis_svc, "run_analysis", down)
    enable(user_id, daily_limit=1)
    autoapply.autoapply_tick()
    await drain()  # attempt 1 re-raises (retryable), attempt 2 is final
    [app] = apps(user_id)
    assert app.status == ApplicationStatus.failed and "письмо" in app.reason
    assert paused_reason(user_id) == ""


def test_one_users_error_does_not_stop_others(user_id, setup, applier, monkeypatch):
    with session_scope() as s:
        bob = User(username="bob", password_hash="x")
        s.add(bob)
        s.flush()
        bob_id = bob.id
    enable(user_id)
    enable(bob_id)
    real = autoapply.plan
    seen = []

    def flaky(s, uid):
        seen.append(uid)
        if uid == user_id:
            raise RuntimeError("bad data")
        return real(s, uid)

    monkeypatch.setattr(autoapply, "plan", flaky)
    autoapply.autoapply_tick()
    assert seen == [user_id, bob_id]


# --------------------------------------------------------------------------- access and switches


def test_cannot_touch_other_users_applications(user_id, setup, applier):
    enable(user_id)
    with session_scope() as s:
        app_id = autoapply.plan(s, user_id)[0]
        other = User(username="bob", password_hash="x")
        s.add(other)
        s.flush()
        for action in (autoapply.approve, autoapply.cancel, autoapply.restore):
            with pytest.raises(NotFound):
                action(s, other.id, app_id)


def test_global_switch_disables_everything(user_id, setup, applier, env):
    enable(user_id)
    env.apply.enabled = False
    assert autoapply.autoapply_tick() == []
    with session_scope() as s:
        assert "администратором" in autoapply.idle_reason(s, user_id)


def test_deactivating_user_forgets_site_sessions(user_id, applier):
    from app.services import admin as admin_svc

    with session_scope() as s:
        bob = User(username="bob", password_hash="x")
        s.add(bob)
        s.flush()
        bob_id = bob.id
    applier.save_session(bob_id, {"cookies": [{"name": "a", "value": "1", "domain": ".hh.ru"}]})
    with session_scope() as s:
        admin_svc.set_active(s, user_id, bob_id, False)
    assert not applier.has_session(bob_id)


# --------------------------------------------------------------------------- web


def _client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app(start_background=False))


def test_web_page_and_actions(user_id, setup, applier, env):
    with _client() as client:
        client.post("/login", data={"username": "alice", "password": "password123"})
        r = client.get("/autoapply")
        assert r.status_code == 200 and "Автоотклики выключены" in r.text and "site-login hh alice" in r.text
        r = client.post("/autoapply/settings", data={"enabled": "true", "mode": "confirm", "min_score": "80",
                                                      "daily_limit": "5", "min_interval_min": "3",
                                                      "active_from_hour": "0", "active_to_hour": "24",
                                                      "letter_tone": "formal", "site_resume_title": "Python"})
        assert "автоотклики работают" in r.text
        with session_scope() as s:
            cfg = autoapply.get_config(s, user_id)
            assert (cfg.min_interval_s, cfg.mode, cfg.letter_tone, cfg.daily_limit, cfg.site_resume_title) == \
                (180, "confirm", "formal", 5, "Python")
        r = client.post("/autoapply/plan")
        assert "В очередь добавлено: 3" in r.text
        with session_scope() as s:
            first = s.scalars(select(Application).order_by(Application.score.desc())).first()
        r = client.post(f"/autoapply/{first.id}/approve")
        assert "Подтверждено" in r.text
        with session_scope() as s:
            assert s.get(Application, first.id).status == ApplicationStatus.approved
        r = client.post(f"/autoapply/{first.id}/cancel")
        assert "Не откликаемся" in r.text and "Вернуть в очередь" in r.text
        client.post(f"/autoapply/{first.id}/restore")
        with session_scope() as s:
            assert s.get(Application, first.id).status == ApplicationStatus.queued
        assert client.post("/autoapply/pause").status_code == 200
        assert paused_reason(user_id) == "вы поставили паузу"
        client.post("/logout")
        with session_scope() as s:
            from app.core.security import hash_password

            s.add(User(username="bob", password_hash=hash_password("password123")))
        client.post("/login", data={"username": "bob", "password": "password123"})
        assert client.post(f"/autoapply/{first.id}/cancel").status_code == 404
        assert client.post(f"/autoapply/{first.id}/approve").status_code == 404


def test_web_settings_errors_do_not_change_config(user_id, setup, applier):
    with _client() as client:
        client.post("/login", data={"username": "alice", "password": "password123"})
        r = client.post("/autoapply/settings", data={"enabled": "true", "mode": "auto", "active_from_hour": "22",
                                                      "active_to_hour": "6", "letter_tone": "friendly"})
        assert "меньше" in r.text
        with session_scope() as s:
            assert not autoapply.get_config(s, user_id).enabled


GOOD_SESSION = b'{"cookies": [{"name": "hhtoken", "value": "1", "domain": ".hh.ru"}]}'


@pytest.mark.parametrize("payload,word", [
    (b"x" * (600 * 1024), "слишком большой"),
    (b"\xff\xfe", "повреждён"),
    (b"not json", "повреждён"),
    (b"[" * 100_000, "повреждён"),
    (b"[]", "не тот файл"),
    (b'{"cookies": "x"}', "не тот файл"),
    (b'{"cookies": [{"name": "a", "value": "1", "domain": "hh.ru.evil.com"}]}', "нет cookies"),
    (b'{"cookies": [{"name": "a", "value": "1", "domain": "evilhh.ru"}]}', "нет cookies"),
])
def test_bad_session_upload_keeps_existing(user_id, applier, payload, word):
    with _client() as client:
        client.post("/login", data={"username": "alice", "password": "password123"})
        ok = client.post("/autoapply/session/hh", files={"file": ("s.json", GOOD_SESSION, "application/json")})
        assert "Вход сохранён" in ok.text
        bad = client.post("/autoapply/session/hh", files={"file": ("s.json", payload, "application/json")})
        assert word in bad.text.lower() or word in bad.text
        assert applier.load_session(user_id)["cookies"][0]["name"] == "hhtoken"
        assert client.post("/autoapply/session/nope", files={"file": ("s.json", GOOD_SESSION)}).status_code == 404


def test_session_upload_after_login_pause_resumes(user_id, applier):
    enable(user_id)
    with session_scope() as s:
        autoapply.pause(s, user_id, "вход в hh.ru истёк")
    with _client() as client:
        client.post("/login", data={"username": "alice", "password": "password123"})
        r = client.post("/autoapply/session/hh", files={"file": ("s.json", GOOD_SESSION, "application/json")})
        assert "автоотклики продолжены" in r.text
        assert paused_reason(user_id) == ""
        client.post("/autoapply/session/hh/delete")
        assert not applier.has_session(user_id)


# --------------------------------------------------------------------------- Telegram


class FakeTelegram:
    def __init__(self, fail_first=0, status=429):
        self.sent, self.fail_first, self.status = [], fail_first, status

    def __call__(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if method == "sendMessage" and self.fail_first:
            self.fail_first -= 1
            return httpx.Response(self.status, json={"ok": False, "error_code": self.status,
                                                     "description": "Too Many Requests: retry after 1",
                                                     "parameters": {"retry_after": 0}})
        self.sent.append((method, params))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def messages(self):
        return [p for m, p in self.sent if m == "sendMessage"]


def _api(fake):
    from app.bot.api import TelegramAPI

    return TelegramAPI("1:X", transport=httpx.MockTransport(fake))


async def _press(api, data):
    from app.bot.handlers import handle_update

    await handle_update(api, {"update_id": 1, "callback_query": {
        "id": "c", "data": data, "from": {}, "message": {"chat": {"id": 555}, "message_id": 9,
                                                        "reply_markup": {"inline_keyboard": [[
                                                            {"text": "✅", "callback_data": data}]]}}}})


async def test_bot_confirm_flow_and_reports(user_id, setup, applier, env):
    from app.bot.digest import send_autoapply_updates

    fake = FakeTelegram()
    api = _api(fake)
    with session_scope() as s:
        s.get(User, user_id).telegram_chat_id = 555
    enable(user_id, mode="confirm", daily_limit=2)
    autoapply.autoapply_tick()
    assert await send_autoapply_updates(api) == 0  # letters aren't written yet: nothing to approve
    await drain()
    assert await send_autoapply_updates(api) == 2  # one message per application, with its letter
    msg = fake.messages()[0]
    assert "Здравствуйте! Мой опыт" in msg["text"] and "Откликнуться?" in msg["text"]
    rows = json.loads(msg["reply_markup"])["inline_keyboard"]
    approve_data = rows[0][0]["callback_data"]
    assert approve_data.startswith("aa:") and rows[1][0]["url"].startswith("https://hh.ru/vacancy/")
    assert await send_autoapply_updates(api) == 0  # not asked twice

    await _press(api, approve_data)
    edit = [p for m, p in fake.sent if m == "editMessageReplyMarkup"][-1]
    assert "Подтверждено" in json.loads(edit["reply_markup"])["inline_keyboard"][0][0]["text"]
    assert len(autoapply.autoapply_tick()) == 1
    await drain()
    assert await send_autoapply_updates(api) == 1
    report = fake.messages()[-1]["text"]
    assert report.startswith("📨") and "✅" in report and 'href="https://hh.ru/vacancy/' in report

    applier.results = [ApplyResult("blocked", "hh.ru показал <капчу>")]
    with session_scope() as s:
        second = s.scalars(select(Application).where(Application.status == ApplicationStatus.queued)).first()
        autoapply.approve(s, user_id, second.id)
    rewind()
    autoapply.autoapply_tick()
    await drain()
    await send_autoapply_updates(api)
    notices = [p["text"] for p in fake.messages() if "на паузе" in p["text"]]
    assert len(notices) == 1 and "&lt;капчу&gt;" in notices[0]
    await send_autoapply_updates(api)
    assert len([p for p in fake.messages() if "на паузе" in p["text"]]) == 1  # once


async def test_bot_pause_notice_retried_after_transient_error(user_id, applier):
    from app.bot.digest import send_autoapply_updates

    fake = FakeTelegram(fail_first=1)
    api = _api(fake)
    with session_scope() as s:
        s.get(User, user_id).telegram_chat_id = 555
    enable(user_id)
    with session_scope() as s:
        autoapply.pause(s, user_id, "капча")
    await send_autoapply_updates(api)
    assert fake.messages() == []
    await send_autoapply_updates(api)
    assert any("на паузе" in p["text"] for p in fake.messages())


async def test_bot_messages_escape_dynamic_values(user_id, setup, applier):
    from app.bot.digest import send_autoapply_updates

    fake = FakeTelegram()
    api = _api(fake)
    with session_scope() as s:
        s.get(User, user_id).telegram_chat_id = 555
        v = s.get(vacancy_svc.Vacancy, setup["vacancies"][0])
        v.title, v.company = "A & B <script>", "<b>X"
    applier.results = [ApplyResult("failed", "<" * 300)]
    enable(user_id, daily_limit=1)
    await cycle()
    await send_autoapply_updates(api)
    text = fake.messages()[-1]["text"]
    assert "&amp;" in text and "&lt;script&gt;" in text and "<script>" not in text


async def test_bot_callbacks_are_scoped_to_the_linked_user(user_id, setup, applier):
    fake = FakeTelegram()
    api = _api(fake)
    enable(user_id)
    with session_scope() as s:
        app_id = autoapply.plan(s, user_id)[0]
        bob = User(username="bob", password_hash="x", telegram_chat_id=555)
        s.add(bob)
    for data in (f"aa:{app_id}", f"ax:{app_id}", "aa:abc"):
        await _press(api, data)
    assert statuses(user_id)[0] == "queued"
    answers = [p.get("text", "") for m, p in fake.sent if m == "answerCallbackQuery"]
    assert all("Не найдено" in a for a in answers)


async def test_bot_pause_resume_and_status(user_id, setup, applier):
    from app.bot.handlers import handle_update

    fake = FakeTelegram()
    api = _api(fake)
    with session_scope() as s:
        s.get(User, user_id).telegram_chat_id = 555
    enable(user_id)

    async def say(text):
        await handle_update(api, {"update_id": 2, "message": {"chat": {"id": 555, "type": "private"},
                                                              "text": text, "from": {}}})
        return fake.messages()[-1]["text"]

    assert "Работают" in await say("/autoapply")
    await _press(api, "ap:1")
    with session_scope() as s:
        cfg = autoapply.get_config(s, user_id)
        assert cfg.paused_reason and cfg.paused_notified  # no duplicate pause notice
    assert "На паузе" in await say("/autoapply")
    await _press(api, "ar:1")
    assert paused_reason(user_id) == ""


def test_doctor_reports_autoapply_state(user_id, applier):
    from app.doctor import check_autoapply

    assert check_autoapply()[0].detail == "ни у кого не включены"
    enable(user_id)
    [check] = check_autoapply()
    assert check.ok and check.name == "автоотклики alice"
    with session_scope() as s:
        autoapply.pause(s, user_id, "капча")
    [check] = check_autoapply()
    assert not check.ok and "капча" in check.detail
