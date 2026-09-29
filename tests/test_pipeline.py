"""End-to-end: features, search pipeline and web UI with the fake LLM provider."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import session_scope
from app.features import FEATURES
from app.jobs.queue import drain
from app.llm.providers.fake import FakeProvider
from app.models import Analysis, Job, JobStatus, SavedSearch, UserVacancy
from app.services import analysis as analysis_svc
from app.services import resumes as resume_svc
from app.services import search as search_svc
from app.services import vacancies as vacancy_svc
from app.sources import registry
from app.sources.base import JobSource, SearchQuery, VacancyDraft

from .conftest import RESUME_TEXT, VACANCY_TEXT

PROFILE = {
    "headline": "Python backend", "seniority": "middle", "years_experience": 4,
    "roles": ["Python-разработчик"], "skills": [{"name": "Python", "level": "core"},
                                                {"name": "FastAPI", "level": "core"}],
    "domains": [], "languages": [], "locations": [], "work_format": "remote",
    "salary_expectation": None, "search_queries": ["python fastapi"], "negative_keywords": ["1С"],
}


class StubSource(JobSource):
    name = "stub"
    title = "Stub"

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        return [
            VacancyDraft(source="stub", external_id="1", title="Python-разработчик",
                         description="Python, FastAPI, PostgreSQL", remote=True, is_partial=True),
            VacancyDraft(source="stub", external_id="2", title="Программист 1С", description="1С"),
            VacancyDraft(source="stub", external_id="3", title="Бариста", description="Кофе"),
        ]

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        return VacancyDraft(source="stub", external_id=external_id, title="Python-разработчик",
                            description="ПОЛНОЕ описание: Python, FastAPI", remote=True)


@pytest.fixture
def stub_source(monkeypatch, env):
    from app.core.config import SourceConfig

    monkeypatch.setitem(registry.SOURCE_CLASSES, "stub", StubSource)
    monkeypatch.setitem(env.sources, "stub", SourceConfig())


@pytest.fixture
def resume_id(user_id) -> int:
    with session_scope() as s:
        return resume_svc.create(s, user_id, title="CV", text=RESUME_TEXT,
                                 preferences="только удалёнка").id


@pytest.mark.parametrize("kind", sorted(FEATURES))
async def test_every_feature_runs(kind, user_id, resume_id):
    with session_scope() as s:
        vacancy_id = vacancy_svc.create_manual(s, user_id, title="Python dev", text=VACANCY_TEXT).id
    feature = FEATURES[kind]
    a = await analysis_svc.run_analysis(
        user_id, kind,
        resume_id=resume_id if feature.needs_resume else None,
        vacancy_id=vacancy_id if feature.needs_vacancy else None,
    )
    assert a.id and a.output
    if feature.score_field:
        assert a.score is not None


async def test_cover_letter_uses_previous_match(user_id, resume_id):
    with session_scope() as s:
        vacancy_id = vacancy_svc.create_manual(s, user_id, title="Python dev", text=VACANCY_TEXT).id
    FakeProvider.canned["match"] = {
        "score": 80, "verdict": "good", "recommendation": "apply", "summary": "ok",
        "matched": ["FastAPI"], "gaps": [], "risks": [], "talking_points": ["ускорил каталог в 3 раза"],
    }
    await analysis_svc.run_analysis(user_id, "match", resume_id=resume_id, vacancy_id=vacancy_id)
    await analysis_svc.run_analysis(user_id, "cover_letter", resume_id=resume_id,
                                    vacancy_id=vacancy_id, params={"tone": "formal"})
    prompt = FakeProvider.calls[-1]
    assert "ускорил каталог в 3 раза" in prompt.user and "Тон: formal" in prompt.user


async def test_search_pipeline(user_id, resume_id, stub_source, env):
    FakeProvider.canned["resume_profile"] = PROFILE
    env.matching.prefilter_min = 10
    with session_scope() as s:
        search_id = search_svc.create(s, user_id, resume_id=resume_id, name="", sources=["stub"],
                                      queries=["python"], filters={}).id
    search_svc.enqueue_search_run(search_id, user_id)
    assert await drain() >= 2  # search_run + match job(s)

    with session_scope() as s:
        jobs = s.scalars(select(Job)).all()
        assert all(j.status == JobStatus.done for j in jobs), [(j.kind, j.error) for j in jobs]
        run = next(j for j in jobs if j.kind == "search_run")
        # 1С is excluded by the profile's negative keywords; barista stays but scores 0,
        # so only the Python vacancy passes prefilter_min and goes to the LLM.
        assert run.result["unique"] == 2
        assert run.result["llm_match_enqueued"] == 1
        assert run.result["queries"] == ["python", "python fastapi"]
        matches = s.scalars(select(Analysis).where(Analysis.kind == "match")).all()
        assert len(matches) == 1
        uvs = s.scalars(select(UserVacancy)).all()
        assert len(uvs) == 3
        assert s.get(SavedSearch, search_id).last_run_at is not None
        items = search_svc.results(s, user_id, search_id)
        assert items[0]["match"] is not None
        # partial vacancy was fetched in full before LLM matching
        assert "ПОЛНОЕ описание" in items[0]["vacancy"].description

    # Second run doesn't re-match the same vacancy.
    search_svc.enqueue_search_run(search_id, user_id)
    await drain()
    with session_scope() as s:
        assert len(s.scalars(select(Analysis).where(Analysis.kind == "match")).all()) == 1


def test_web_flow(user_id, env):
    from app.main import create_app

    with TestClient(create_app(start_background=False)) as client:
        assert client.get("/resumes", follow_redirects=False).headers["location"] == "/login"
        r = client.post("/login", data={"username": "alice", "password": "wrong"}, follow_redirects=True)
        assert "Неверный логин" in r.text
        client.post("/login", data={"username": "alice", "password": "password123"})

        r = client.post("/resumes", data={"title": "CV", "text": RESUME_TEXT})
        assert r.status_code == 200 and "Оценка резюме" in r.text
        resume_id = int(re.search(r"/resumes/(\d+)", str(r.url)).group(1))

        r = client.post("/vacancies", data={"mode": "text", "title": "Python dev", "text": VACANCY_TEXT})
        vacancy_id = int(re.search(r"/vacancies/(\d+)", str(r.url)).group(1))
        assert "Сопроводительное письмо" in r.text

        r = client.post("/analyses", data={"kind": "cover_letter", "resume_id": resume_id,
                                           "vacancy_id": vacancy_id, "p_tone": "concise",
                                           "back": f"/vacancies/{vacancy_id}"})
        assert "/jobs/" in str(r.url) and "в очереди" in r.text

        import asyncio

        asyncio.run(drain())
        job_id = int(re.search(r"/jobs/(\d+)", str(r.url)).group(1))
        r = client.get(f"/jobs/{job_id}")
        assert "/analyses/" in str(r.url) and "Скопировать" in r.text

        for page in ("/resumes", f"/resumes/{resume_id}", "/vacancies", f"/vacancies/{vacancy_id}",
                     "/searches", "/jobs", "/usage"):
            assert client.get(page).status_code == 200, page

        # Another user's objects are invisible.
        client.post("/logout")
        from app.core.security import hash_password
        from app.models import User

        with session_scope() as s:
            s.add(User(username="bob", password_hash=hash_password("password123")))
        client.post("/login", data={"username": "bob", "password": "password123"})
        assert client.get(f"/resumes/{resume_id}").status_code == 404
        assert client.get(f"/vacancies/{vacancy_id}").status_code == 404
        assert client.get(f"/jobs/{job_id}").status_code == 404


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/", "http://169.254.169.254/latest/meta-data",
                                 "http://localhost/", "http://10.0.0.5/"])
async def test_url_import_blocks_internal_addresses(url):
    from app.jobs.queue import JobError

    with pytest.raises(JobError, match="внутренние|порты"):
        await vacancy_svc.draft_from_url(url)


async def test_scheduler_enqueues_due_searches_once(user_id, resume_id, stub_source):
    from app.jobs.scheduler import enqueue_due_searches

    with session_scope() as s:
        search_svc.create(s, user_id, resume_id=resume_id, name="auto", sources=["stub"],
                          queries=[], filters={}, interval_minutes=60)
        search_svc.create(s, user_id, resume_id=resume_id, name="manual", sources=["stub"],
                          queries=[], filters={}, interval_minutes=0)
    assert len(enqueue_due_searches()) == 1
    assert enqueue_due_searches() == []  # already queued
    FakeProvider.canned["resume_profile"] = PROFILE
    await drain()
    assert enqueue_due_searches() == []  # ran just now, not due for an hour


def test_dedup_key_normalisation():
    k = vacancy_svc.make_dedup_key
    assert k("ООО «Ромашка»", "Python-разработчик (Middle)") == k("Ромашка", "Python разработчик")
    assert k("Ромашка", "Python-разработчик") != k("Лютик", "Python-разработчик")
    assert k(None, "Python") is None


class DupSource(StubSource):
    name = "dup"

    async def search(self, query, limit):
        return [
            VacancyDraft(source="dup", external_id="a", title="Python-разработчик", company="ООО Ромашка",
                         description="Python, FastAPI"),
            VacancyDraft(source="dup", external_id="b", title="Python-разработчик (удалённо)",
                         company="Ромашка", description="Python, FastAPI, remote"),
        ]


async def test_search_merges_cross_source_duplicates(user_id, resume_id, monkeypatch, env):
    from app.core.config import SourceConfig

    monkeypatch.setitem(registry.SOURCE_CLASSES, "dup", DupSource)
    monkeypatch.setitem(env.sources, "dup", SourceConfig())
    FakeProvider.canned["resume_profile"] = PROFILE
    with session_scope() as s:
        search_id = search_svc.create(s, user_id, resume_id=resume_id, name="", sources=["dup"],
                                      queries=[], filters={}).id
    result = await search_svc.run_search(user_id, search_id)
    assert result["unique"] == 1 and result["duplicates"] == 1 and result["llm_match_enqueued"] == 1


def test_login_is_throttled(user_id):
    from app.main import create_app
    from app.web.routes.auth import throttle

    throttle._failures.clear()
    with TestClient(create_app(start_background=False)) as client:
        for _ in range(5):
            client.post("/login", data={"username": "alice", "password": "bad"})
        r = client.post("/login", data={"username": "alice", "password": "password123"})
        assert "Слишком много попыток" in r.text
    throttle._failures.clear()
