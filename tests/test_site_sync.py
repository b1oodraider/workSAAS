"""hh.ru application statuses -> tracker (read-only sync)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.apply import registry as apply_registry
from app.apply.base import SessionExpired, SiteApplication
from app.apply.hh_browser import HHBrowserApplier
from app.apply.hh_status import classify, fetch_negotiations, parse_negotiations
from app.core.db import session_scope
from app.jobs.queue import drain
from app.models import Job, UserVacancy, Vacancy
from app.services import site_sync
from app.services import vacancies as vacancy_svc
from app.sources.base import VacancyDraft

SAMPLE = (Path(__file__).parent / "fixtures" / "hh_status" / "negotiations.html").read_text(encoding="utf-8")
SESSION = {"cookies": [{"name": "hhtoken", "value": "1", "domain": ".hh.ru", "path": "/"}]}
ITEM = '[data-qa*="negotiations-item"]'


def test_parse_by_item_selector_and_by_links():
    by_item = {i.external_id: i for i in parse_negotiations(SAMPLE, ITEM)}
    assert {k: v.status for k, v in by_item.items()} == {
        "101": "applied", "102": "viewed", "103": "invited", "104": "rejected", "105": "archived"}
    assert by_item["101"].title == "Python-разработчик" and by_item["101"].company
    # Markup changed (selector finds nothing): cards are found around vacancy links.
    by_link = {i.external_id: i.status for i in parse_negotiations(SAMPLE, '[data-qa="gone"]')}
    assert by_link["103"] == "invited" and by_link["104"] == "rejected"


def test_classify_prefers_latest_news():
    assert classify("Приглашение … Отказ") == "rejected"
    assert classify("Не просмотрен") == "applied"
    assert classify("Просмотрен работодателем") == "viewed"


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_pages_until_nothing_new(monkeypatch):
    monkeypatch.setattr("app.apply.hh_status.PAGE_PAUSE_S", 0)
    pages = []

    def handler(request):
        assert request.headers["cookie"] == "hhtoken=1"
        pages.append(request.url.params["page"])
        return httpx.Response(200, text=SAMPLE)  # page 1 repeats page 0: stop

    items = await fetch_negotiations(SESSION, base_url="https://hh.ru", item_selector=ITEM, login_selector="",
                                     use_proxy=False, client=_client(handler))
    assert pages == ["0", "1"] and len(items) == 5


async def test_fetch_detects_logged_out_session():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://hh.ru/account/login?backurl=/applicant"})

    with pytest.raises(SessionExpired):
        await fetch_negotiations(SESSION, base_url="https://hh.ru", item_selector=ITEM, login_selector="",
                                 use_proxy=False, client=_client(handler))


def test_statuses_move_forward_only(user_id):
    with session_scope() as s:
        def hh(ext, status):
            v, _ = vacancy_svc.upsert(s, VacancyDraft(source="hh", external_id=ext, title=f"V{ext}", description="x"))
            vacancy_svc.attach(s, user_id, v.id)
            vacancy_svc.set_status(s, user_id, v.id, status)
            return v.id

        offer, interview, fresh = hh("1", "offer"), hh("2", "interview"), hh("3", "new")
        counts = site_sync.apply_statuses(s, user_id, "hh", [
            SiteApplication("1", "rejected", "V1"),   # never overrides an offer
            SiteApplication("2", "viewed", "V2"),     # never moves back
            SiteApplication("3", "invited", "V3"),
            SiteApplication("4", "applied", "Откликнулся вручную", "Acme", "https://hh.ru/vacancy/4"),
            SiteApplication("5", "archived", "Закрыта"),
        ])
        st = {uv.vacancy_id: uv.status.value for uv in s.scalars(select(UserVacancy))}
        manual = s.scalar(select(Vacancy).where(Vacancy.external_id == "4"))
        assert s.scalar(select(Vacancy).where(Vacancy.external_id == "5")) is None
        assert st[manual.id] == "applied" and manual.is_partial
    assert counts == {"added": 1, "updated": 2, "invited": 1, "rejected": 0}
    assert st[offer] == "offer" and st[interview] == "interview" and st[fresh] == "interview"


class FakeHH(HHBrowserApplier):
    def __init__(self, result):
        super().__init__()
        self.result = result

    async def site_applications(self, user_id):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def hh(monkeypatch, env, tmp_path, user_id):
    env.data_dir = str(tmp_path)

    def install(result):
        fake = FakeHH(result)
        monkeypatch.setitem(apply_registry.APPLIERS, "hh", fake)
        fake.save_session(user_id, SESSION)
        return fake

    return install


async def test_periodic_sync_runs_once_per_interval(user_id, hh, env):
    hh([SiteApplication("7", "invited", "Python dev")])
    assert len(site_sync.enqueue_due_syncs()) == 1
    assert site_sync.enqueue_due_syncs() == []  # one per interval
    await drain()
    with session_scope() as s:
        job = s.scalar(select(Job).where(Job.kind == "site_sync"))
        assert job.result["invited"] == 1 and job.result["added"] == 1
    env.apply.status_sync_hours = 0
    assert site_sync.enqueue_due_syncs() == []


async def test_expired_session_is_marked_stale(user_id, hh):
    fake = hh(SessionExpired("вход истёк"))
    site_sync.enqueue_sync(user_id)
    await drain()
    assert not fake.session_ok(user_id)
    assert site_sync.enqueue_due_syncs() == []  # no point retrying until the user logs in again


def test_no_session_no_sync(user_id, env, tmp_path, monkeypatch):
    env.data_dir = str(tmp_path)
    monkeypatch.setitem(apply_registry.APPLIERS, "hh", FakeHH([]))
    assert site_sync.enqueue_due_syncs() == []


def test_parser_ignores_recommendations_tabs_and_foreign_links():
    page = """<html><body>
    <nav><a href="/applicant/negotiations?state=discard">Отказ</a> <a href="/x">Приглашения</a></nav>
    <div><div><a href="/vacancy/1">Python dev</a> <span>Просмотрен</span></div></div>
    <section>Рекомендуем: <a href="/vacancy/2">Java dev</a></section>
    <div><a href="https://evil.example/x/vacancy/3">fake</a> <span>Приглашение</span></div>
    <div><a href="/vacancy/4">Отказоустойчивые системы, инженер</a> <span>Не просмотрен</span></div>
    </body></html>"""
    got = {i.external_id: i.status for i in parse_negotiations(page, '[data-qa="gone"]')}
    assert got == {"1": "viewed", "4": "applied"}


def test_inner_selector_matches_do_not_duplicate_cards():
    page = """<div data-qa="negotiations-item"><div data-qa="negotiations-item-title">
      <a href="/vacancy/9">Python</a></div><span>Отказ</span></div>"""
    assert [(i.external_id, i.status) for i in parse_negotiations(page, ITEM)] == [("9", "rejected")]


def test_sync_updates_the_card_the_user_tracks(user_id):
    """Search keeps one copy per dedup key: the hh status must land on that card, not a duplicate."""
    with session_scope() as s:
        habr, _ = vacancy_svc.upsert(s, VacancyDraft(source="habr", external_id="h", title="Python dev",
                                                     company="Acme", description="x"))
        vacancy_svc.upsert(s, VacancyDraft(source="hh", external_id="55", title="Python dev", company="Acme",
                                           description="x"))
        vacancy_svc.attach(s, user_id, habr.id)
        vacancy_svc.set_status(s, user_id, habr.id, "applied")
        site_sync.apply_statuses(s, user_id, "hh", [SiteApplication("55", "invited", "Python dev", "Acme")])
        rows = list(s.scalars(select(UserVacancy).where(UserVacancy.user_id == user_id)))
    assert [(r.vacancy_id, r.status.value) for r in rows] == [(habr.id, "interview")]


async def test_manual_sync_is_not_duplicated(user_id, hh):
    hh([])
    first = site_sync.enqueue_sync(user_id, manual=True)
    assert site_sync.enqueue_sync(user_id, manual=True) == first  # still queued
    await drain()
    assert site_sync.enqueue_sync(user_id, manual=True) == first  # just done: no hammering hh.ru
