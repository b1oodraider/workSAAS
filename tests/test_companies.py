"""Company rating from the instance's own tracker data and vacancy reviews."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.db import session_scope, utcnow
from app.models import Analysis, User
from app.services import companies as company_svc
from app.services import vacancies as vacancy_svc
from app.services.errors import ValidationFailed
from app.sources.base import VacancyDraft

TEXT = "Ищем менеджера по продажам. Задачи: работа с клиентами, CRM. Опыт от года. " * 3
_seq = iter(range(1, 10_000))


def _users(s, n: int) -> list[int]:
    users = [User(username=f"u{next(_seq)}", password_hash="!") for _ in range(n)]
    s.add_all(users)
    s.flush()
    return [u.id for u in users]


def _vacancy(s, user_id: int, company: str, source: str = "hh") -> int:
    n = next(_seq)
    if source == "manual":
        v = vacancy_svc.create_manual(s, user_id, title=f"Менеджер {n}", text=TEXT)
        v.company = company
        return v.id
    v, _ = vacancy_svc.upsert(s, VacancyDraft(source=source, external_id=str(n), title=f"Менеджер {n}",
                                              company=company, description=TEXT))
    vacancy_svc.attach(s, user_id, v.id)
    return v.id


def _applied(s, user_id: int, vid: int, days_ago: float, answer: str | None = None,
             after_days: float = 0, quality: str = "") -> None:
    vacancy_svc.set_status(s, user_id, vid, "applied")
    _, uv = vacancy_svc.get_for_user(s, user_id, vid)
    uv.applied_at = utcnow() - timedelta(days=days_ago)
    if answer:
        at = uv.applied_at + timedelta(days=after_days)
        uv.status = answer
        uv.status_history = [*uv.status_history, {"status": answer, "at": at.isoformat(timespec="seconds")}]
        uv.response_quality = quality


def test_responsive_company_needs_three_people_and_gets_coarse_badges():
    with session_scope() as s:
        a, b, c = _users(s, 3)
        _applied(s, a, _vacancy(s, a, "ООО «Ромашка»"), 30, "interview", after_days=2)
        _applied(s, b, _vacancy(s, b, "Ромашка"), 30, "rejected", after_days=1, quality="reasoned")
        with session_scope() as s2:
            two = company_svc.ratings(s2, ["Ромашка"])
        _applied(s, c, _vacancy(s, c, "Ромашка (Москва)"), 30, "offer", after_days=3)
    assert two == {}  # two people are not an aggregate: nothing about their applications leaks
    with session_scope() as s:
        r = company_svc.ratings(s, ["Ромашка"])[company_svc.key("Ромашка")]
    assert r.badges == ["обычно отвечает на отклики", "отвечает в первые дни"]
    assert r.score == 100 and r.sort_adjustment == 5
    assert not any(ch.isdigit() for ch in " ".join(r.badges))  # no counts or dates


def test_silent_company_counts_each_person_once():
    with session_scope() as s:
        a, b, c = _users(s, 3)
        for _ in range(5):  # one person's five applications are still one voice
            _applied(s, a, _vacancy(s, a, "Лютик"), 30, "interview", after_days=1)
        _applied(s, b, _vacancy(s, b, "Лютик"), 30)  # b never marks answers (auto-apply): not silence
        _applied(s, c, _vacancy(s, c, "Лютик"), 20, "rejected", after_days=18, quality="template")
        _applied(s, c, _vacancy(s, c, "Лютик"), 3)  # c's latest: too early, c does not count yet
    with session_scope() as s:
        assert company_svc.ratings(s, ["Лютик"]) == {}
        # Once b marks answers somewhere, b's month of silence from Лютик is a signal.
        _applied(s, b, _vacancy(s, b, "Другая"), 10, "rejected", after_days=1)
        _applied(s, c, _vacancy(s, c, "Лютик"), 1, "rejected", after_days=0.5, quality="template")
    with session_scope() as s:
        r = company_svc.ratings(s, ["Лютик"])[company_svc.key("Лютик")]
    assert "отвечает примерно на половину откликов" in r.badges
    assert "отказы обычно без объяснений" not in r.badges  # 1 explained (invite) vs 1 template
    assert r.score is not None


def test_untrusted_sources_and_repeated_reviews_do_not_count():
    with session_scope() as s:
        a, b, c = _users(s, 3)
        for u in (a, b, c):  # anyone can type any company name into a manual vacancy
            vid = _vacancy(s, u, "Кактус", source="manual")
            _applied(s, u, vid, 30)
            vacancy_svc.set_match_vote(s, u, vid, -1, "company")
        fake = _vacancy(s, a, "Кактус", source="manual")
        s.add(Analysis(user_id=a, kind="vacancy_review", vacancy_id=fake, params={}, output={"overall_score": 5},
                       provider="fake", model="fake", prompt_version="1"))
        real = _vacancy(s, a, "Кактус")
        for score in (10, 90):  # only the latest review of a vacancy counts
            s.add(Analysis(user_id=a, kind="vacancy_review", vacancy_id=real, params={},
                           output={"overall_score": score}, provider="fake", model="fake", prompt_version="1"))
    with session_scope() as s:
        assert company_svc.ratings(s, ["Кактус"]) == {}  # one reviewed vacancy is not enough to label a company
        s.add(Analysis(user_id=a, kind="vacancy_review", vacancy_id=_vacancy(s, a, "Кактус"), params={},
                       output={"overall_score": 70}, provider="fake", model="fake", prompt_version="1"))
    with session_scope() as s:
        r = company_svc.ratings(s, ["Кактус"])[company_svc.key("Кактус")]
    assert r.badges == ["понятные описания вакансий"] and r.score == 80


def test_company_dislikes_need_three_people():
    with session_scope() as s:
        users = _users(s, 3)
        for u in users[:2]:
            vacancy_svc.set_match_vote(s, u, _vacancy(s, u, "Мак"), -1, "company")
    with session_scope() as s:
        assert company_svc.ratings(s, ["Мак"]) == {}
        vacancy_svc.set_match_vote(s, users[2], _vacancy(s, users[2], "Мак"), -1, "company")
    with session_scope() as s:
        r = company_svc.ratings(s, ["Мак"])[company_svc.key("Мак")]
    assert r.badges == ["не нравится нескольким пользователям"] and r.sort_adjustment < 0


def test_vote_and_response_quality_validation(user_id):
    with session_scope() as s:
        vid = _vacancy(s, user_id, "Кактус")
        with pytest.raises(ValidationFailed):
            vacancy_svc.set_match_vote(s, user_id, vid, 1, "grade")  # a reason only goes with 👎
        with pytest.raises(ValidationFailed):
            vacancy_svc.set_match_vote(s, user_id, vid, -1, "whatever")
        with pytest.raises(ValidationFailed):
            vacancy_svc.set_response_quality(s, user_id, vid, "invite")
        vacancy_svc.set_response_quality(s, user_id, vid, "reasoned")
