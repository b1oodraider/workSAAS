"""Foreign job sources (Himalayas, Jobicy, Arbeitnow, Working Nomads, The Muse, Adzuna, HN).

Fixtures in tests/fixtures/sources/ are trimmed real responses fetched on 2026-10-08,
except adzuna.json, which is synthetic (written from the docs, no API key). No network here:
``fetch_json`` is monkeypatched in every search test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.config import SourceConfig
from app.sources import adzuna, arbeitnow, himalayas, hn_hiring, jobicy, themuse, workingnomads
from app.sources.base import SearchQuery, SourceError, safe_map

FIXTURES = Path(__file__).parent / "fixtures" / "sources"


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def fake_fetch(responses, calls: list | None = None):
    """fetch_json stand-in: ``responses`` is a value or a callable(url, params) -> value."""
    async def fetch(url, *, params=None, **kwargs):
        if calls is not None:
            calls.append((url, dict(params or {}), kwargs))
        return responses(url, params or {}) if callable(responses) else responses
    return fetch


# --- Himalayas -------------------------------------------------------------------------------

def test_himalayas_items():
    drafts = safe_map(himalayas.item_to_draft, load("himalayas")["jobs"])
    assert len(drafts) == 3
    d = drafts[0]
    assert (d.title, d.company) == ("Software Engineer", "ElitevoraSys LLC")
    assert d.url == "https://himalayas.app/companies/elitevorasys-llc/jobs/software-engineer"
    assert d.external_id == "software-engineer"
    assert (d.salary_from, d.salary_to, d.currency) == (160000, 240000, "USD") and d.remote
    assert d.location == "United States" and d.experience == "Senior" and d.employment == "Full Time"
    assert "Зарплата указана в год" in d.description and d.published_at.year == 2026
    no_salary = drafts[1]
    assert no_salary.salary_from is None and no_salary.currency is None
    assert drafts[2].currency == "INR"
    src = himalayas.HimalayasSource(SourceConfig())
    assert src.external_id_from_url("https://himalayas.app/companies/acme/jobs/python-dev-123?ref=x") == "python-dev-123"
    assert src.external_id_from_url("https://example.com/jobs/1") is None


async def test_himalayas_search(monkeypatch):
    calls: list = []
    monkeypatch.setattr(himalayas, "fetch_json", fake_fetch(load("himalayas"), calls))
    drafts = await himalayas.HimalayasSource(SourceConfig()).search(SearchQuery(text="python"), 2)
    assert len(drafts) == 2
    url, params, kwargs = calls[0]
    assert params == {"q": "python", "limit": 2} and kwargs["cache_ttl"] == 3600
    await himalayas.HimalayasSource(SourceConfig()).search(SearchQuery(text="python"), 50)
    assert calls[1][1]["limit"] == himalayas.PAGE_MAX


# --- Jobicy ----------------------------------------------------------------------------------

def test_jobicy_items():
    drafts = safe_map(jobicy.item_to_draft, load("jobicy")["jobs"])
    assert [d.external_id for d in drafts] == ["154780", "152668", "143728"]
    first, paid = drafts[0], drafts[1]
    assert first.title == "Senior Software Engineer, Fullstack" and first.company == "CoderPad"
    assert first.url == "https://jobicy.com/jobs/154780-senior-software-engineer-fullstack-4"
    assert first.salary_from is None and first.currency is None and first.remote
    assert first.location == "LATAM" and first.employment == "Contract"
    assert (paid.salary_from, paid.salary_to, paid.currency) == (114500, 152500, "USD")
    assert "Зарплата указана в год" in paid.description
    assert drafts[2].title == "Workflow Developer & Tester"
    legacy = jobicy.item_to_draft({"id": 1, "jobTitle": "Dev", "annualSalaryMin": "90000",
                                   "annualSalaryMax": "120000", "salaryCurrency": "EUR"})
    assert (legacy.salary_from, legacy.salary_to, legacy.currency) == (90000, 120000, "EUR")
    src = jobicy.JobicySource(SourceConfig())
    assert src.external_id_from_url("https://jobicy.com/jobs/154780-senior-software-engineer") == "154780"


async def test_jobicy_search_and_empty_result(monkeypatch):
    calls: list = []
    monkeypatch.setattr(jobicy, "fetch_json", fake_fetch(load("jobicy"), calls))
    drafts = await jobicy.JobicySource(SourceConfig()).search(SearchQuery(text="python"), 1)
    assert len(drafts) == 1 and calls[0][1] == {"count": 1, "tag": "python"}
    # Real "nothing found" answer: success with no "jobs" key.
    monkeypatch.setattr(jobicy, "fetch_json", fake_fetch({"jobCount": 0, "success": True,
                                                          "message": "No jobs found"}))
    assert await jobicy.JobicySource(SourceConfig()).search(SearchQuery(text="zzz"), 10) == []


# --- Arbeitnow -------------------------------------------------------------------------------

def test_arbeitnow_items():
    drafts = safe_map(arbeitnow.item_to_draft, load("arbeitnow")["data"])
    assert len(drafts) == 3
    remote, dev = drafts[0], drafts[1]
    assert remote.remote is True and remote.company == "KnowWay GmbH"
    assert dev.title == "Full Stack Developer" and dev.remote is None and dev.salary_from is None
    assert dev.external_id == "full-stack-developer-germany-munich-93633"
    assert dev.url == "https://www.arbeitnow.com/jobs/companies/bookboost/full-stack-developer-germany-munich-93633"
    assert dev.employment == "Experienced, Permanent, Full time" and "full stack" in dev.skills
    assert dev.published_at.year == 2026
    src = arbeitnow.ArbeitnowSource(SourceConfig())
    assert src.external_id_from_url(dev.url) == dev.external_id


async def test_arbeitnow_filters_locally_and_paginates(monkeypatch):
    page1 = load("arbeitnow")
    page2 = {"data": [{"slug": "python-dev-2", "title": "Python Developer", "tags": [], "created_at": 1791000000}],
             "links": {"next": None}}
    calls: list = []
    monkeypatch.setattr(arbeitnow, "fetch_json",
                        fake_fetch(lambda _u, p: page1 if p["page"] == 1 else page2, calls))
    drafts = await arbeitnow.ArbeitnowSource(SourceConfig()).search(SearchQuery(text="developer"), 10)
    assert [c[1]["page"] for c in calls] == [1, 2]
    assert {d.external_id for d in drafts} == {"full-stack-developer-germany-munich-93633", "python-dev-2"}
    # Enough matches on page 1 -> page 2 is not requested; the limit is respected.
    calls.clear()
    drafts = await arbeitnow.ArbeitnowSource(SourceConfig()).search(SearchQuery(text="developer"), 1)
    assert len(drafts) == 1 and len(calls) == 1
    # options.pages caps the scan.
    calls.clear()
    await arbeitnow.ArbeitnowSource(SourceConfig(options={"pages": 1})).search(SearchQuery(text="nothing"), 10)
    assert len(calls) == 1


# --- Working Nomads --------------------------------------------------------------------------

def test_workingnomads_items():
    drafts = safe_map(workingnomads.item_to_draft, load("workingnomads"))
    d = drafts[0]
    assert (d.external_id, d.title, d.company) == ("1923073", "Senior Vue Developer", "Lemon.io")
    assert d.url == "https://www.workingnomads.com/job/go/1923073/" and d.remote
    assert d.skills == ["vuejs", "php", "dotnet", "nodejs", "english"] and d.salary_from is None
    assert d.description.startswith("Требования к локации кандидата: Europe")
    odd = workingnomads.item_to_draft({"url": "https://www.workingnomads.com/jobs/some-slug", "title": "X"})
    assert odd.external_id and len(odd.external_id) == 24
    src = workingnomads.WorkingNomadsSource(SourceConfig())
    assert src.external_id_from_url("https://www.workingnomads.com/job/go/1885527/") == "1885527"


async def test_workingnomads_filters_locally(monkeypatch):
    monkeypatch.setattr(workingnomads, "fetch_json", fake_fetch(load("workingnomads") + ["junk"]))
    drafts = await workingnomads.WorkingNomadsSource(SourceConfig()).search(SearchQuery(text="golang"), 10)
    assert [d.title for d in drafts] == ["Senior back-end Engineer"]  # matched by tags
    drafts = await workingnomads.WorkingNomadsSource(SourceConfig()).search(SearchQuery(text="developer"), 1)
    assert len(drafts) == 1


# --- The Muse --------------------------------------------------------------------------------

def test_themuse_items():
    drafts = safe_map(themuse.item_to_draft, load("themuse")["results"])
    remote, onsite = drafts[0], drafts[1]
    assert (remote.title, remote.company) == ("Staff Software Engineer, Backend", "GlossGenius")
    assert remote.url == "https://www.themuse.com/jobs/glossgenius/staff-software-engineer-backend-7d1ec3"
    assert remote.remote is True and remote.location == "Flexible / Remote; New York, NY"
    assert remote.experience == "Senior Level" and remote.salary_from is None
    assert onsite.remote is None and onsite.company == "SpaceX"


async def test_themuse_categories_pages_and_filter(monkeypatch):
    calls: list = []
    monkeypatch.setattr(themuse, "fetch_json", fake_fetch(load("themuse"), calls))
    cfg = SourceConfig(options={"categories": ["Software Engineering", "Data and Analytics"], "pages": 2,
                                "location": "Flexible / Remote"})
    drafts = await themuse.TheMuseSource(cfg).search(SearchQuery(text="full stack"), 10)
    assert [(c[1]["category"], c[1]["page"]) for c in calls] == [
        ("Software Engineering", 0), ("Software Engineering", 1), ("Data and Analytics", 0), ("Data and Analytics", 1)]
    assert all(c[1]["location"] == "Flexible / Remote" for c in calls)
    # Same jobs on every page/category are deduplicated; "Shipping Coordinator" is filtered out.
    assert [d.external_id for d in drafts] == ["21852956"]


async def test_themuse_partial_failure(monkeypatch):
    def respond(_url, params):
        if params["category"] == "Broken":
            raise SourceError("The Muse ответил 500")
        return load("themuse")
    monkeypatch.setattr(themuse, "fetch_json", fake_fetch(respond))
    cfg = SourceConfig(options={"categories": ["Broken", "Software Engineering"], "pages": 1})
    assert len(await themuse.TheMuseSource(cfg).search(SearchQuery(text="engineer"), 10)) == 2
    with pytest.raises(SourceError):
        await themuse.TheMuseSource(SourceConfig(options={"categories": "Broken"})).search(
            SearchQuery(text="engineer"), 10)


# --- Adzuna (synthetic fixture) --------------------------------------------------------------

def test_adzuna_items():
    drafts = safe_map(lambda i: adzuna.item_to_draft(i, "gb"), load("adzuna")["results"])
    assert len(drafts) == 2  # the item without id is skipped
    d = drafts[0]
    assert d.title == "Senior Python Developer - Remote" and d.company == "Acme Data Ltd"
    assert (d.salary_from, d.salary_to, d.currency) == (70000, 85000, "GBP")
    assert d.remote and d.is_partial and d.location == "London, UK"
    assert d.url.startswith("https://www.adzuna.co.uk/jobs/land/ad/4931520873")
    assert drafts[1].currency is None and drafts[1].employment == "частичная занятость"
    src = adzuna.AdzunaSource(SourceConfig())
    assert src.external_id_from_url(d.url) == "4931520873"
    assert src.external_id_from_url("https://www.adzuna.de/details/123456") == "123456"


async def test_adzuna_requires_key(monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    with pytest.raises(SourceError, match="API-ключ"):
        await adzuna.AdzunaSource(SourceConfig()).search(SearchQuery(text="python"), 10)


async def test_adzuna_search_countries(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id1")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key1")
    calls: list = []

    def respond(url, _params):
        if "/pl/" in url:
            raise SourceError("Adzuna ответил 500")
        return load("adzuna")
    monkeypatch.setattr(adzuna, "fetch_json", fake_fetch(respond, calls))
    cfg = SourceConfig(options={"countries": ["gb", "de", "pl", "../x"]})
    drafts = await adzuna.AdzunaSource(cfg).search(SearchQuery(text="python"), 3)
    assert [c[0] for c in calls] == [adzuna.API.format(country=c) for c in ("gb", "de", "pl")]
    params = calls[0][1]
    assert (params["app_id"], params["app_key"], params["what"], params["results_per_page"]) == ("id1", "key1", "python", 3)
    assert len(drafts) == 3 and {d.currency for d in drafts if d.salary_from} <= {"GBP", "EUR"}


# --- Hacker News: Who is hiring --------------------------------------------------------------

def test_hn_latest_thread_and_items():
    data = load("hn_hiring")
    assert hn_hiring.latest_thread_id(data["stories"]) == "49922569"  # not "Who wants to be hired?"
    assert hn_hiring.latest_thread_id({"hits": [{"objectID": "1", "title": "Ask HN: Other"}]}) is None
    drafts = safe_map(hn_hiring.item_to_draft, data["comments"]["hits"])
    d = drafts[1]
    assert d.title == "Flow Traders | Research Engineer | Hong Kong | Full Time | Visa | Relocation"
    assert d.company == "Flow Traders" and d.url == "https://news.ycombinator.com/item?id=49986633"
    assert d.remote is None and d.salary_from is None
    assert drafts[0].company == "Manifest" and drafts[0].remote is True
    assert "https://www.manifestcyber.com/" in drafts[0].title  # HTML entities decoded
    assert not hn_hiring.HNHiringSource.trusted
    src = hn_hiring.HNHiringSource(SourceConfig())
    assert src.external_id_from_url("https://news.ycombinator.com/item?id=49986633") == "49986633"


async def test_hn_search_keeps_top_level_posts(monkeypatch):
    data = load("hn_hiring")
    reply = {"objectID": "50000001", "parent_id": 49987144, "story_id": 49922569,
             "comment_text": "Is this role open to EU?", "created_at": "2026-10-07T03:00:00Z"}
    comments = {**data["comments"], "hits": data["comments"]["hits"] + [reply]}
    calls: list = []
    monkeypatch.setattr(hn_hiring, "fetch_json",
                        fake_fetch(lambda url, _p: data["stories"] if "search_by_date" in url else comments, calls))
    drafts = await hn_hiring.HNHiringSource(SourceConfig()).search(SearchQuery(text="python"), 2)
    assert [d.external_id for d in drafts] == ["49987144", "49986633"]  # newest first, reply dropped
    assert calls[1][1]["tags"] == "comment,story_49922569" and calls[1][1]["query"] == "python"


async def test_hn_search_without_thread(monkeypatch):
    monkeypatch.setattr(hn_hiring, "fetch_json", fake_fetch({"hits": []}))
    assert await hn_hiring.HNHiringSource(SourceConfig()).search(SearchQuery(text="python"), 5) == []


# --- Malformed items are skipped, never break a batch ----------------------------------------

@pytest.mark.parametrize("parse, bad", [
    (himalayas.item_to_draft, {"guid": "https://himalayas.app/companies/a/jobs/b", "title": "T", "minSalary": "n/a"}),
    (jobicy.item_to_draft, {"id": 1, "jobTitle": "T", "jobType": 5, "pubDate": 12}),
    (arbeitnow.item_to_draft, {"slug": "s", "title": "T", "tags": "not-a-list", "created_at": "bad"}),
    (workingnomads.item_to_draft, {"url": 5, "title": ["T"]}),
    (themuse.item_to_draft, {"id": 1, "name": "T", "refs": "x", "company": 5}),
    (adzuna.item_to_draft, {"id": 1, "title": "T", "salary_min": "abc", "company": "x"}),
    (hn_hiring.item_to_draft, {"objectID": "1", "comment_text": {"html": "x"}}),
])
def test_malformed_items_skipped(parse, bad):
    good_and_bad = [None, "junk", 42, {}, bad]
    drafts = safe_map(parse, good_and_bad, source="test")
    # Only ``bad`` may survive, and only as a valid draft (lenient parsing of odd fields).
    assert len(drafts) <= 1
    for d in drafts:
        assert d.title and d.external_id
