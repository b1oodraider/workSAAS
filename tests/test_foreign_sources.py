"""Foreign job boards: API items -> drafts (fixtures follow the live API shape of 2026-10)."""

from __future__ import annotations

import pytest

from app.core.config import SourceConfig
from app.sources import foreign
from app.sources.base import SearchQuery
from app.sources.foreign import (
    ArbeitnowSource,
    arbeitnow_to_draft,
    himalayas_to_draft,
    jobicy_to_draft,
)


def test_himalayas_item():
    d = himalayas_to_draft({
        "title": "Senior Java Engineer", "companyName": "Acme", "minSalary": 90000, "maxSalary": None,
        "currency": "USD", "seniority": ["Senior"], "locationRestrictions": ["Germany", "Poland"],
        "categories": ["Java-Developer"], "description": "<p>Build <b>APIs</b></p>", "pubDate": 1789127040,
        "employmentType": "Full Time",
        "applicationLink": "https://himalayas.app/companies/acme/jobs/senior-java-engineer-123",
    })
    assert d.external_id == "senior-java-engineer-123" and d.remote and d.salary_from == 90000
    assert d.currency == "USD" and d.salary_to is None and d.experience == "Senior"
    assert "Страны: Germany, Poland" in d.description and "Build APIs" in d.description
    assert d.published_at.year == 2026 and d.published_at.tzinfo is None
    assert himalayas_to_draft({"title": "No link"}) is None


def test_jobicy_item_without_salary():
    d = jobicy_to_draft({"id": 152522, "jobTitle": "Senior Accountant", "companyName": "Zuora", "jobGeo": "USA",
                         "jobLevel": "Senior", "jobType": ["Full-Time"], "jobIndustry": ["Accounting"],
                         "jobDescription": "<p>IFRS</p>", "pubDate": "2026-09-04T13:33:04+03:00"})
    assert d.external_id == "152522" and d.location == "удалённо: USA" and d.currency is None
    assert d.published_at.hour == 10  # converted to UTC, not just stripped


async def test_arbeitnow_filters_locally_and_stops_at_last_page(monkeypatch):
    pages = {1: {"data": [{"slug": "java-dev-1", "title": "Java Developer", "tags": ["Backend"], "remote": True,
                           "location": "Berlin", "created_at": 1789127040, "description": "<p>x</p>"},
                          {"slug": "cook-2", "title": "Koch", "tags": [], "remote": False}],
                 "links": {"next": "p2"}},
             2: {"data": [{"slug": "java-dev-3", "title": "Senior Java Engineer", "tags": []}], "links": {"next": None}}}
    calls = []

    async def fake_fetch(url, *, params=None, **_):
        calls.append(params["page"])
        return pages[params["page"]]

    monkeypatch.setattr(foreign, "fetch_json", fake_fetch)
    drafts = await ArbeitnowSource(SourceConfig()).search(SearchQuery(text="java"), 10)
    assert [d.external_id for d in drafts] == ["java-dev-1", "java-dev-3"] and calls == [1, 2]
    assert drafts[0].location == "Berlin, удалённо" and drafts[0].remote
    assert arbeitnow_to_draft({"title": "x"}) is None


@pytest.mark.parametrize("name", ["himalayas", "jobicy", "arbeitnow"])
def test_foreign_sources_are_registered_but_not_preselected(name):
    from app.sources.registry import SOURCE_CLASSES

    cls = SOURCE_CLASSES[name]
    assert cls.trusted and not cls.checked_by_default
