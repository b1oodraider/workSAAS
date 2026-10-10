"""Candidate search criteria and adjacent roles / soft grade matching."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.db import session_scope
from app.features.resume_profile.schema import ResumeProfile, Skill
from app.llm.providers.fake import FakeProvider
from app.models import SavedSearch
from app.ranking.prefilter import KeywordRanker, RankInput, RankProfile
from app.services import analysis as analysis_svc
from app.services import criteria as criteria_svc
from app.services import resumes as resume_svc
from app.services import search as search_svc
from app.services import vacancies as vacancy_svc
from app.services.errors import NotFound, ValidationFailed
from app.sources.base import SearchFilters

from .conftest import RESUME_TEXT, VACANCY_TEXT


@pytest.fixture
def resume_id(user_id) -> int:
    with session_scope() as s:
        return resume_svc.create(s, user_id, title="CV", text=RESUME_TEXT, preferences="без 1С").id


def test_save_load_and_missing(user_id, resume_id):
    with session_scope() as s:
        assert criteria_svc.missing(criteria_svc.get(s, user_id, resume_id))[0].startswith("Минимальная")
        criteria_svc.save(s, user_id, resume_id, {
            "salary_min": 250000, "work_formats": ["remote"], "countries": "Россия, Казахстан, россия",
            "grades": ["middle", "senior"], "languages": "английский B2", "stretch_up": True})
    with session_scope() as s:
        c = criteria_svc.get(s, user_id, resume_id)
    assert c.countries == ["Россия", "Казахстан"] and c.work_formats == ["remote"]
    assert not any("зарплата" in q.lower() for q in criteria_svc.missing(c))
    with session_scope() as s, pytest.raises(ValidationFailed):
        criteria_svc.save(s, user_id, resume_id, {"salary_min": 300000, "salary_desired": 200000})
    with session_scope() as s, pytest.raises(ValidationFailed):
        criteria_svc.save(s, user_id, resume_id, {"work_formats": ["moon"]})


def test_broken_stored_criteria_never_break_search():
    assert criteria_svc.load({"salary_min": "много"}) == criteria_svc.SearchCriteria()
    assert criteria_svc.load(None) == criteria_svc.SearchCriteria()


def test_effective_filters_only_tighten():
    c = criteria_svc.SearchCriteria(salary_min=250000, work_formats=["remote"], industries_avoid=["гемблинг"])
    f = criteria_svc.effective_filters(SearchFilters(salary_min=300000, exclude_words=["1С"]), c)
    assert f.salary_min == 300000 and f.remote_only and f.exclude_words == ["1С", "гемблинг"]
    usd = criteria_svc.effective_filters(SearchFilters(), criteria_svc.SearchCriteria(salary_min=5000,
                                                                                      salary_currency="USD"))
    assert usd.salary_min is None  # the prefilter's salary floor is in rubles only


async def test_criteria_reach_the_match_prompt(user_id, resume_id):
    with session_scope() as s:
        criteria_svc.save(s, user_id, resume_id, {"salary_min": 250000, "countries": ["Германия"]})
        vid = vacancy_svc.create_manual(s, user_id, title="Python dev", text=VACANCY_TEXT).id
    await analysis_svc.run_analysis(user_id, "match", resume_id=resume_id, vacancy_id=vid)
    system = FakeProvider.calls[-1].system
    assert "250 000 RUB" in system and "Германия" in system and "без 1С" in system
    assert system.index("<preferences>") < system.index("250 000") < system.index("</preferences>")


def test_adjacent_roles_get_partial_title_credit():
    profile = RankProfile(core_skills=["Python", "SQL"], roles=["Backend Developer"],
                          adjacent_roles=["Data Engineer"])
    r = KeywordRanker()
    main = r.score(profile, RankInput("Senior Backend Developer", "Python SQL"), SearchFilters())
    adjacent = r.score(profile, RankInput("Data Engineer", "Python SQL"), SearchFilters())
    other = r.score(profile, RankInput("Product Manager", "Python SQL"), SearchFilters())
    assert main.score > adjacent.score > other.score
    assert any("смежная" in reason for reason in adjacent.reasons)


def test_queries_include_extra_and_adjacent_roles():
    profile = ResumeProfile(headline="x", seniority="middle", years_experience=3, roles=["Python-разработчик"],
                            adjacent_roles=["Data Engineer", "Platform Engineer", "MLOps"],
                            skills=[Skill(name="Python", level="core")], domains=[], work_format="remote",
                            search_queries=["python", "python django", "backend python"])
    search = SavedSearch(queries=["fastapi"])
    crit = criteria_svc.SearchCriteria(roles_extra=["Tech Lead"])
    queries = search_svc.build_queries(search, profile, crit)
    assert queries[0] == "fastapi" and "Tech Lead" in queries and "Data Engineer" in queries
    assert "MLOps" not in queries  # only a couple of adjacent roles: they cost LLM money downstream
    no_adj = search_svc.build_queries(search, profile, crit.model_copy(update={"adjacent_roles": False}))
    assert "Data Engineer" not in no_adj


def test_criteria_route_saves_and_checks_owner(user_id, resume_id):
    from app.core.security import hash_password
    from app.main import create_app
    from app.models import User

    with TestClient(create_app(start_background=False)) as client:
        client.post("/login", data={"username": "alice", "password": "password123"})
        r = client.post(f"/resumes/{resume_id}/criteria", data={
            "salary_min": "250 000", "work_formats": ["remote", "hybrid"], "countries": "Россия",
            "stretch_up": "on", "relocation": "abroad"})
        assert r.status_code == 200 and "Условия поиска сохранены" in r.text
    with session_scope() as s:
        c = criteria_svc.get(s, user_id, resume_id)
        assert c.salary_min == 250000 and c.work_formats == ["remote", "hybrid"] and not c.adjacent_roles
        s.add(User(username="bob", password_hash=hash_password("password123")))
    with TestClient(create_app(start_background=False)) as client:
        client.post("/login", data={"username": "bob", "password": "password123"})
        assert client.post(f"/resumes/{resume_id}/criteria", data={"salary_min": "1"}).status_code == 404
    with session_scope() as s, pytest.raises(NotFound):
        criteria_svc.get(s, user_id + 1, resume_id)


async def test_letters_never_see_the_salary_floor(user_id, resume_id):
    with session_scope() as s:
        criteria_svc.save(s, user_id, resume_id, {"salary_min": 250000, "salary_desired": 320000,
                                                  "citizenship": "РФ", "deal_breakers": "овертаймы"})
        vid = vacancy_svc.create_manual(s, user_id, title="Python dev", text=VACANCY_TEXT).id
    for kind in ("cover_letter", "recruiter_reply", "offer_negotiation"):
        params = {"message": "Какие ожидания по зарплате?"} if kind == "recruiter_reply" else (
            {"offer": "200 000 на руки, удалёнка", "desired": "300 000"} if kind == "offer_negotiation" else {})
        await analysis_svc.run_analysis(user_id, kind, resume_id=resume_id, vacancy_id=vid, params=params)
        prompt = FakeProvider.calls[-1].system + FakeProvider.calls[-1].user
        assert "250 000" not in prompt and "РФ" not in prompt and "овертаймы" not in prompt, kind
        assert "320 000" in prompt, kind  # the desired salary is fine to name
    await analysis_svc.run_analysis(user_id, "match", resume_id=resume_id, vacancy_id=vid)
    assert "250 000" in FakeProvider.calls[-1].system  # matching does use the floor


@pytest.mark.parametrize("raw,value", [("250 000 ₽", 250000), ("250000", 250000), ("250к", 250000),
                                       ("", None), ("250 000 руб.", 250000)])
def test_money_input(raw, value):
    from app.web.routes.resumes import _money_input

    assert _money_input(raw) == value


@pytest.mark.parametrize("raw", ["²", "много", "1" * 12])
def test_money_input_rejects_nonsense(raw):
    from app.web.routes.resumes import _money_input

    with pytest.raises(ValidationFailed):
        _money_input(raw)


def test_criteria_limits_are_listed():
    c = criteria_svc.SearchCriteria(salary_min=250000, work_formats=["remote"], industries_avoid=["гемблинг"])
    assert criteria_svc.limits(c) == ["зарплата от 250 000 ₽", "только удалёнка", "в тексте: гемблинг"]
