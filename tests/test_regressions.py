"""Regression tests for issues found by the test-critic review."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import ModelPrice
from app.core.db import session_scope, utcnow
from app.core.security import hash_password
from app.features import FEATURES
from app.jobs.queue import JobContext, drain, enqueue, job_handler, recover_stale_jobs
from app.services.search import enqueue_due_searches
from app.llm import get_gateway
from app.llm.base import BudgetExceeded, LLMUnavailable
from app.llm.providers.fake import FakeProvider
from app.models import Analysis, Job, JobStatus, LLMUsage, SavedSearch, User, UserVacancy
from app.services import admin as admin_svc
from app.services import analysis as analysis_svc
from app.services import resumes as resume_svc
from app.services import search as search_svc
from app.services import vacancies as vacancy_svc
from app.sources import registry
from app.sources.base import JobSource, SourceError, VacancyDraft

from .conftest import RESUME_TEXT, VACANCY_TEXT
from .test_pipeline import PROFILE, StubSource


@pytest.fixture
def resume_id(user_id) -> int:
    with session_scope() as s:
        return resume_svc.create(s, user_id, title="CV", text=RESUME_TEXT).id


@pytest.fixture
def vacancy_id(user_id) -> int:
    with session_scope() as s:
        return vacancy_svc.create_manual(s, user_id, title="Python dev", text=VACANCY_TEXT).id


def _stub(monkeypatch, env, cls=StubSource, name="stub"):
    from app.core.config import SourceConfig

    monkeypatch.setitem(registry.SOURCE_CLASSES, name, cls)
    monkeypatch.setitem(env.sources, name, SourceConfig())


# --------------------------------------------------------------------------- jobs


def test_recover_stale_jobs_respects_max_attempts(env, user_id):
    with session_scope() as s:
        s.add(Job(kind="analysis", user_id=user_id, status=JobStatus.running, attempts=env.jobs.max_attempts))
        s.add(Job(kind="analysis", user_id=user_id, status=JobStatus.running, attempts=1))
    recover_stale_jobs()
    with session_scope() as s:
        statuses = sorted(j.status.value for j in s.scalars(select(Job)))
    assert statuses == ["failed", "queued"]


_calls = {"n": 0}


@job_handler("test_flaky")
async def _flaky(ctx: JobContext) -> dict:
    _calls["n"] += 1
    raise LLMUnavailable("down")


@job_handler("test_budget")
async def _budget(ctx: JobContext) -> dict:
    _calls["n"] += 1
    raise BudgetExceeded("бюджет исчерпан")


async def test_retryable_errors_retry_then_fail_budget_does_not(env, user_id):
    _calls["n"] = 0
    job_id = enqueue("test_flaky", {}, user_id=user_id)
    await drain()
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.failed and job.attempts == env.jobs.max_attempts and "down" in job.error
    _calls["n"] = 0
    job_id = enqueue("test_budget", {}, user_id=user_id)
    await drain()
    with session_scope() as s:
        assert s.get(Job, job_id).attempts == 1 and _calls["n"] == 1


# --------------------------------------------------------------------------- features


async def test_pair_features_tolerate_stale_prior_outputs(user_id, resume_id, vacancy_id):
    with session_scope() as s:
        for kind in ("match", "vacancy_review"):
            s.add(Analysis(user_id=user_id, kind=kind, resume_id=resume_id if kind == "match" else None,
                           vacancy_id=vacancy_id, output={"summary": "old"}, provider="f", model="m",
                           prompt_version="0"))
    for kind in ("cover_letter", "follow_up", "interview_prep", "tailor_resume"):
        a = await analysis_svc.run_analysis(user_id, kind, resume_id=resume_id, vacancy_id=vacancy_id)
        assert a.id


@pytest.mark.parametrize("kind", sorted(FEATURES))
async def test_every_feature_result_page_renders(kind, user_id, resume_id, vacancy_id):
    from app.main import create_app

    from .test_pipeline import SAMPLE_PARAMS

    feature = FEATURES[kind]
    a = await analysis_svc.run_analysis(
        user_id, kind, resume_id=resume_id if feature.needs_resume else None,
        vacancy_id=vacancy_id if feature.needs_vacancy else None, params=SAMPLE_PARAMS.get(kind))
    with TestClient(create_app(start_background=False)) as client:
        client.post("/login", data={"username": "alice", "password": "password123"})
        r = client.get(f"/analyses/{a.id}")
        assert r.status_code == 200 and feature.title in r.text


# --------------------------------------------------------------------------- budget


async def test_budget_stops_bulk_matching(env, user_id, resume_id, monkeypatch):
    _stub(monkeypatch, env)
    FakeProvider.canned["resume_profile"] = PROFILE
    await search_svc.get_profile(user_id, resume_id)  # profile computed before the budget is set
    env.llm.prices["fake-model"] = ModelPrice(input=1_000_000.0, output=0.0)
    with session_scope() as s:
        s.get(User, user_id).monthly_budget_usd = 1.0

    class Many(StubSource):
        async def search(self, query, limit):
            return [VacancyDraft(source="stub", external_id=str(i), title="Python-разработчик",
                                 description=f"Python FastAPI {i}") for i in range(3)]

    _stub(monkeypatch, env, Many)
    with session_scope() as s:
        sid = search_svc.create(s, user_id, resume_id=resume_id, name="", sources=["stub"],
                                queries=[], filters={}).id
    search_svc.enqueue_search_run(sid, user_id)
    await drain()
    with session_scope() as s:
        matches = s.scalars(select(Analysis).where(Analysis.kind == "match")).all()
        failed = s.scalars(select(Job).where(Job.kind == "analysis", Job.status == JobStatus.failed)).all()
    assert len(matches) == 1 and len(failed) == 2 and all("бюджет" in j.error for j in failed), [(j.status, j.error) for j in failed]


async def test_budget_resets_monthly(env, user_id):
    with session_scope() as s:
        s.get(User, user_id).monthly_budget_usd = 1.0
        s.add(LLMUsage(user_id=user_id, task="x", provider="p", model="m", cost_usd=5.0,
                       created_at=utcnow() - timedelta(days=40)))
    gw = get_gateway()
    assert gw.month_spent(user_id) == 0
    await gw.run(FEATURES["vacancy_review"].task, {"vacancy": "V"}, user_id=user_id)


# --------------------------------------------------------------------------- search & scheduler


async def test_rerun_before_matches_finish_does_not_duplicate(env, user_id, resume_id, monkeypatch):
    _stub(monkeypatch, env)
    FakeProvider.canned["resume_profile"] = PROFILE
    with session_scope() as s:
        sid = search_svc.create(s, user_id, resume_id=resume_id, name="", sources=["stub"],
                                queries=[], filters={}).id
    first = await search_svc.run_search(user_id, sid)
    second = await search_svc.run_search(user_id, sid)
    assert first["llm_match_enqueued"] == 1 and second["llm_match_enqueued"] == 0


def test_scheduler_skips_blocked_users(env, user_id, resume_id, monkeypatch):
    _stub(monkeypatch, env)
    with session_scope() as s:
        search_svc.create(s, user_id, resume_id=resume_id, name="", sources=["stub"], queries=[],
                          filters={}, interval_minutes=60)
        other = User(username="admin2", password_hash="x", is_admin=True)
        s.add(other)
        s.flush()
        admin_svc.set_active(s, other.id, user_id, False)
    assert enqueue_due_searches() == []


class BrokenSource(JobSource):
    name = "broken"
    title = "Сломанный"

    async def search(self, query, limit):
        raise SourceError("недоступен")


async def test_failed_search_run_not_rescheduled_immediately(env, user_id, resume_id, monkeypatch):
    _stub(monkeypatch, env, BrokenSource, "broken")
    FakeProvider.canned["resume_profile"] = PROFILE
    with session_scope() as s:
        search_svc.create(s, user_id, resume_id=resume_id, name="", sources=["broken"], queries=[],
                          filters={}, interval_minutes=60)
    assert len(enqueue_due_searches()) == 1
    await drain()
    with session_scope() as s:
        job = s.scalars(select(Job).where(Job.kind == "search_run")).one()
        assert job.status == JobStatus.failed and "Источники недоступны" in job.error
    assert enqueue_due_searches() == []


# --------------------------------------------------------------------------- tracker


async def test_tracker_rejection_clears_followups(user_id, vacancy_id, tg_api):
    from app.bot.digest import send_reminders

    with session_scope() as s:
        s.get(User, user_id).telegram_chat_id = 555
        vacancy_svc.set_status(s, user_id, vacancy_id, "applied")
        vacancy_svc.set_status(s, user_id, vacancy_id, "rejected")
        _, uv = vacancy_svc.get_for_user(s, user_id, vacancy_id)
        assert uv.next_action_at is None
        vacancy_svc.set_status(s, user_id, vacancy_id, "rejected", notes="слишком далеко")
        assert uv.notes == "слишком далеко" and len(uv.status_history) == 2
        data = vacancy_svc.tracker(s, user_id)
    assert await send_reminders(tg_api) == 0
    assert data["funnel"]["applied"] == 1 and data["funnel"]["rejected"] == 1
    assert data["funnel"]["interview_rate"] == 0


@pytest.fixture
def tg_api(env):
    from app.bot.api import TelegramAPI

    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    return TelegramAPI("1:X", transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- access control


@pytest.fixture
def two_users(user_id, resume_id, vacancy_id):
    with session_scope() as s:
        s.add(User(username="bob", password_hash=hash_password("password123")))
        sid = search_svc.create(s, user_id, resume_id=resume_id, name="alice search", sources=["hh"],
                                queries=[], filters={}).id
    analysis = None
    return {"resume": resume_id, "vacancy": vacancy_id, "search": sid, "analysis": analysis}


FOREIGN_ROUTES = [
    ("post", "/vacancies/{vacancy}/status", {"status": "hidden"}),
    ("post", "/vacancies/{vacancy}/next-action", {"when": "2030-01-01T10:00", "note": "x"}),
    ("get", "/searches/{search}", None),
    ("post", "/searches/{search}/run", {}),
    ("post", "/searches/{search}/delete", {}),
    ("post", "/resumes/{resume}/edit", {"title": "hacked", "text": "x" * 100}),
    ("post", "/resumes/{resume}/delete", {}),
    ("get", "/resumes/{resume}", None),
    ("get", "/vacancies/{vacancy}", None),
]


@pytest.mark.parametrize("method,path,data", FOREIGN_ROUTES)
def test_every_id_route_rejects_foreign_objects(method, path, data, two_users):
    from app.main import create_app

    url = path.format(**two_users)
    with TestClient(create_app(start_background=False)) as client:
        client.post("/login", data={"username": "bob", "password": "password123"})
        r = getattr(client, method)(url, **({"data": data} if data is not None else {}), follow_redirects=False)
        assert r.status_code == 404, (url, r.status_code)
    with session_scope() as s:
        uv = s.scalars(select(UserVacancy)).one()
        assert uv.status.value == "new" and uv.next_action_at is None
        assert resume_svc.get_owned(s, 1, two_users["resume"]).title == "CV"
        assert s.get(SavedSearch, two_users["search"]) is not None
        assert s.scalars(select(Job)).all() == []


def test_foreign_ids_in_forms_create_nothing(two_users):
    from app.main import create_app

    with TestClient(create_app(start_background=False)) as client:
        client.post("/login", data={"username": "bob", "password": "password123"})
        client.post("/analyses", data={"kind": "match", "resume_id": two_users["resume"],
                                       "vacancy_id": two_users["vacancy"]})
        client.post("/searches", data={"resume_id": two_users["resume"], "sources": "hh"})
        client.post("/settings/preferences", data={"default_resume_id": two_users["resume"]})
    with session_scope() as s:
        assert s.scalars(select(Job)).all() == []
        assert s.scalars(select(User).where(User.username == "bob")).one().default_resume_id is None


# --------------------------------------------------------------------------- config


def test_example_config_loads(monkeypatch):
    from app.core import config as config_mod
    from app.llm.providers import build_provider
    from app.sources.registry import SOURCE_CLASSES

    monkeypatch.setattr(config_mod, "CONFIG_PATH", config_mod.Path("config.example.toml"))
    for key in list(__import__("os").environ):
        if key.startswith("WS_"):
            monkeypatch.delenv(key)
    settings = config_mod.Settings(_env_file=None)
    for name, route in settings.llm.routes.items():
        for target in route.targets():
            assert target.provider in settings.llm.providers, (name, target.provider)
    for feature in FEATURES.values():
        assert settings.llm.route_for(feature.task.name)
    assert set(settings.sources) <= set(SOURCE_CLASSES)
    assert build_provider("fake", settings.llm.providers["fake"])
    assert json.dumps(settings.model_dump(mode="json"))
