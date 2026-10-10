"""Job-board sources: employer ATS (Greenhouse, Lever, Ashby), We Work Remotely, Djinni, getmatch.

Fixtures in tests/fixtures/sources/ are trimmed real responses (fetched 2026-10-08);
every network call is monkeypatched.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.core.config import SourceConfig
from app.sources import ashby, boards, djinni, getmatch, greenhouse, lever, weworkremotely
from app.sources.base import SearchQuery, SourceError

FIXTURES = Path(__file__).parent / "fixtures" / "sources"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def cfg(**options) -> SourceConfig:
    return SourceConfig(options=options)


def q(text: str) -> SearchQuery:
    return SearchQuery(text=text)


# --- shared helpers -------------------------------------------------------------------------

def test_company_tokens_validated_and_deduplicated():
    assert boards.company_tokens({"companies": ["gitlab", "gitlab", "../admin", "a/b", " elastic "]}) == [
        "gitlab", "elastic"]
    assert boards.company_tokens({"companies": "linear, supabase"}) == ["linear", "supabase"]
    assert boards.company_tokens({}) == []


# --- Greenhouse -----------------------------------------------------------------------------

def test_greenhouse_fixture_to_drafts():
    jobs = load_json("greenhouse.json")["jobs"]
    drafts = [greenhouse.item_to_draft(j, "gitlab") for j in jobs]
    d = drafts[1]
    assert d.source == "greenhouse" and d.external_id == "gitlab/8843512002"
    assert d.title == "Backend Engineer, Create: Repository Management" and d.company == "GitLab"
    assert d.location == "Remote, United Kingdom" and d.remote is True
    assert d.url == "https://job-boards.greenhouse.io/gitlab/jobs/8843512002"
    assert d.published_at.year == 2026 and d.published_at.month == 9
    # content is HTML-escaped HTML: neither tags nor entities may leak into the text
    assert d.description.startswith("Отдел: DevOps Engineering\n\nGitLab")
    assert "<" not in d.description and "&lt;" not in d.description and "&quot;" not in d.description
    assert d.salary_from is None and d.currency is None
    assert drafts[2].title.endswith("(Ruby)")  # trailing spaces trimmed


def test_greenhouse_url_to_id():
    src = greenhouse.GreenhouseSource(cfg())
    assert src.external_id_from_url("https://job-boards.greenhouse.io/gitlab/jobs/8843512002") == "gitlab/8843512002"
    assert src.external_id_from_url("https://boards.greenhouse.io/elastic/jobs/123?gh_src=x") == "elastic/123"
    assert src.external_id_from_url("https://example.com/jobs/1") is None


async def test_greenhouse_search_resilient_to_failing_company(monkeypatch):
    data = load_json("greenhouse.json")
    calls = []

    async def fake_fetch(url, **kw):
        calls.append(url)
        if "/nope/" in url:
            raise SourceError("Greenhouse nope ответил 404")
        return data

    monkeypatch.setattr(greenhouse, "fetch_json", fake_fetch)
    src = greenhouse.GreenhouseSource(cfg(companies=["nope", "gitlab"]))
    drafts = await src.search(q("Ruby"), 10)
    assert len(calls) == 2  # the failing company did not stop the loop
    assert [d.external_id for d in drafts][0] == "gitlab/8687926002"  # title hit first
    assert all("ruby" in f"{d.title} {d.description}".lower() for d in drafts)
    assert len(await src.search(q("backend engineer"), 2)) == 2


async def test_greenhouse_all_companies_failing_raises(monkeypatch):
    async def fake_fetch(url, **kw):
        raise SourceError("down")

    monkeypatch.setattr(greenhouse, "fetch_json", fake_fetch)
    with pytest.raises(SourceError):
        await greenhouse.GreenhouseSource(cfg(companies=["a", "b"])).search(q("python"), 10)


async def test_greenhouse_without_companies_does_not_fetch(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(greenhouse, "fetch_json", boom)
    assert await greenhouse.GreenhouseSource(cfg(companies=[])).search(q("python"), 10) == []
    assert boards.company_tokens({}, greenhouse.DEFAULT_COMPANIES)[:1] == ["gitlab"]  # no option: defaults


async def test_greenhouse_malformed_items_skipped(monkeypatch):
    data = load_json("greenhouse.json")
    data["jobs"] += [{"id": 1}, {"id": 2, "title": "Engineer", "content": 123}, "garbage"]

    async def fake_fetch(url, **kw):
        return data

    monkeypatch.setattr(greenhouse, "fetch_json", fake_fetch)
    drafts = await greenhouse.GreenhouseSource(cfg(companies=["gitlab"])).search(q("engineer"), 10)
    assert len(drafts) == 3


# --- Lever ----------------------------------------------------------------------------------

def test_lever_fixture_to_drafts():
    items = load_json("lever.json")
    d = lever.item_to_draft(items[0], "palantir")
    assert d.external_id == "palantir/10dfc8bc-99ad-4ca2-ab76-853cb90a92c2"
    assert d.title == "Backend Software Engineer - Application Development" and d.company == "palantir"
    assert d.location == "London, United Kingdom" and d.employment == "Full-time"
    assert d.remote is False  # hybrid
    assert d.url == "https://jobs.lever.co/palantir/10dfc8bc-99ad-4ca2-ab76-853cb90a92c2"
    assert d.published_at.year == 2024
    assert d.description.startswith("Команда: Dev\n\nA World-Changing Company")
    assert "Core Responsiblities" in d.description  # lists[] are part of the description
    assert d.salary_from is None
    assert lever.item_to_draft(items[2], "palantir").remote is False  # onsite


def test_lever_salary_range_and_remote():
    item = copy.deepcopy(load_json("lever.json")[0])
    item["workplaceType"] = "remote"
    item["salaryRange"] = {"min": 120000, "max": 150000, "currency": "USD", "interval": "per-year-salary"}
    d = lever.item_to_draft(item, "acme")
    assert (d.salary_from, d.salary_to, d.currency, d.remote) == (120000, 150000, "USD", True)
    assert d.description.startswith("Зарплата в год: 120000–150000 USD")
    item["salaryRange"] = {"min": 50, "max": 70, "currency": "USD", "interval": "per-hour-wage"}
    assert lever.item_to_draft(item, "acme").salary_from is None


async def test_lever_search_resilient_and_skips_malformed(monkeypatch):
    items = load_json("lever.json") + [{"id": "x", "text": "Engineer", "lists": [{"content": 5}]}, 42]

    async def fake_fetch(url, **kw):
        if url.endswith("/gone"):
            raise SourceError("Lever gone ответил 404")
        return items

    monkeypatch.setattr(lever, "fetch_json", fake_fetch)
    src = lever.LeverSource(cfg(companies=["gone", "palantir"]))
    drafts = await src.search(q("defense"), 10)
    assert [d.title for d in drafts] == ["Backend Software Engineer - Defense"]
    assert len(await src.search(q("engineer"), 10)) == 3
    url = "https://jobs.lever.co/palantir/10dfc8bc-99ad-4ca2-ab76-853cb90a92c2"
    assert src.external_id_from_url(url) == "palantir/10dfc8bc-99ad-4ca2-ab76-853cb90a92c2"


# --- Ashby ----------------------------------------------------------------------------------

def test_ashby_fixture_to_drafts_with_compensation():
    jobs = load_json("ashby.json")["jobs"]
    eu, uk, ca = (ashby.item_to_draft(j, "ashby") for j in jobs)
    assert eu.external_id == "ashby/7458d4e9-da2e-47bd-98cb-adfda43d42b2" and eu.title == "Engineering Manager - EU"
    assert (eu.salary_from, eu.salary_to, eu.currency) == (110000, 185000, "EUR")
    assert (uk.salary_from, uk.salary_to, uk.currency) == (110000, 200000, "GBP")
    assert (ca.salary_from, ca.salary_to, ca.currency) == (190000, 280000, "CAD")
    assert eu.remote is True and eu.employment == "Полная занятость"
    assert eu.location.startswith("Remote - European Union; ")
    assert eu.description.startswith("Компенсация в год: €110K – €185K")
    assert "Команда: Engineering / EMEA Engineering" in eu.description and "<p" not in eu.description
    assert eu.url == "https://jobs.ashbyhq.com/ashby/7458d4e9-da2e-47bd-98cb-adfda43d42b2"
    assert eu.published_at.year == 2024


def test_ashby_unlisted_and_no_compensation():
    job = copy.deepcopy(load_json("ashby.json")["jobs"][0])
    job["isListed"] = False
    assert ashby.item_to_draft(job, "ashby") is None
    job.update(isListed=True, compensation=None, isRemote=False, workplaceType="OnSite")
    d = ashby.item_to_draft(job, "ashby")
    assert d.salary_from is None and d.currency is None and d.remote is False
    assert not d.description.startswith("Компенсация")


async def test_ashby_search_and_fetch(monkeypatch):
    data = load_json("ashby.json")
    data["jobs"].append({"id": "broken", "title": "Engineer", "descriptionHtml": 7})

    async def fake_fetch(url, **kw):
        if url.endswith("/missing"):
            raise SourceError("Ashby missing ответил 404")
        return data

    monkeypatch.setattr(ashby, "fetch_json", fake_fetch)
    src = ashby.AshbySource(cfg(companies=["missing", "ashby"]))
    drafts = await src.search(q("canada"), 10)
    assert [d.title for d in drafts] == ["Engineering Manager - Canada"]
    assert len(await src.search(q("engineering manager"), 2)) == 2
    url = "https://jobs.ashbyhq.com/ashby/7458d4e9-da2e-47bd-98cb-adfda43d42b2"
    full = await src.fetch(src.external_id_from_url(url))
    assert full.title == "Engineering Manager - EU"


# --- We Work Remotely -----------------------------------------------------------------------

def test_wwr_feed_to_drafts():
    items = boards.rss_items(load_text("weworkremotely.xml"))
    drafts = [weworkremotely.item_to_draft(i) for i in items]
    d = drafts[0]
    assert d.external_id == "dremio-senior-staff-software-engineer-query-execution"
    assert (d.company, d.title) == ("Dremio", "Senior Staff Software Engineer - Query Execution")
    assert d.location == "Anywhere in the World" and d.remote is True
    assert d.url == "https://weworkremotely.com/remote-jobs/dremio-senior-staff-software-engineer-query-execution"
    assert d.published_at.month == 9 and d.published_at.tzinfo is None
    assert d.description.startswith("Категория: Full-Stack Programming") and "<p>" not in d.description
    assert drafts[2].location == "North America Only"
    assert weworkremotely.split_title("No company here") == (None, "No company here")


async def test_wwr_search_multiple_feeds(monkeypatch):
    xml = load_text("weworkremotely.xml")
    seen = []

    async def fake_feed(url, **kw):
        seen.append(url)
        if "broken" in url:
            raise SourceError("We Work Remotely ответил 500")
        return xml

    monkeypatch.setattr(weworkremotely, "fetch_feed", fake_feed)
    drafts = await weworkremotely.WeWorkRemotelySource(cfg()).search(q("salesforce"), 10)
    assert seen == weworkremotely.DEFAULT_FEEDS
    assert [d.company for d in drafts] == ["Toptal"]  # same job in several feeds -> once
    src = weworkremotely.WeWorkRemotelySource(cfg(feeds=["https://x/broken.rss", "https://x/ok.rss"]))
    assert len(await src.search(q("senior"), 1)) == 1
    with pytest.raises(SourceError):
        await weworkremotely.WeWorkRemotelySource(cfg(feeds=["https://x/broken.rss"])).search(q("x"), 5)


async def test_wwr_malformed_feed_and_items(monkeypatch):
    bad_item = "<rss><channel><item><title>No link</title></item>" \
               "<item><title>A: B</title><link>https://weworkremotely.com/remote-jobs/a-b</link></item>" \
               "</channel></rss>"

    async def fake_feed(url, **kw):
        return bad_item if "one" in url else "<not xml"

    monkeypatch.setattr(weworkremotely, "fetch_feed", fake_feed)
    src = weworkremotely.WeWorkRemotelySource(cfg(feeds=["https://x/one.rss", "https://x/two.rss"]))
    drafts = await src.search(q("b"), 10)
    assert [(d.external_id, d.company, d.title) for d in drafts] == [("a-b", "A", "B")]


def test_wwr_job_page_with_lenient_json_ld():
    # Real WWR pages: raw newlines inside JSON strings, HTML-escaped description, zero salary.
    page = """<script type="application/ld+json">{
      "@context": "http://schema.org/", "@type": "JobPosting",
      "title": "Senior Backend Developer (Python)",
      "description": "&lt;h3&gt;The Role&lt;/h3&gt;&lt;p&gt;Build APIs
      in Python&lt;/p&gt;",
      "hiringOrganization": {"@type": "Organization", "name": "Proxify AB"},
      "baseSalary": {"@type": "MonetaryAmount", "currency": "USD",
                     "value": {"@type": "QuantitativeValue", "minValue": 0, "maxValue": 0}}
    }</script>"""
    d = weworkremotely.parse_job_page(page, "proxify-x", "https://weworkremotely.com/remote-jobs/proxify-x")
    assert d.company == "Proxify AB" and d.remote is True and "<h3>" not in d.description
    assert "The Role" in d.description and d.salary_from is None and d.currency is None
    src = weworkremotely.WeWorkRemotelySource(cfg())
    assert src.external_id_from_url("https://weworkremotely.com/remote-jobs/proxify-x") == "proxify-x"


# --- Djinni ---------------------------------------------------------------------------------

def test_djinni_primary_keyword():
    assert djinni.primary_keyword("Python разработчик") == "Python"
    assert djinni.primary_keyword("Senior Go developer") == "Golang"
    assert djinni.primary_keyword("Senior Data Engineer") == "Data Engineer"
    assert djinni.primary_keyword("Аналитик данных") == "Data Analyst"
    assert djinni.primary_keyword("бэкенд разработчик") is None
    assert djinni.primary_keyword("бэкенд разработчик", {"бэкенд": "Python"}) == "Python"


def test_djinni_feed_to_drafts():
    items = boards.rss_items(load_text("djinni.xml"))
    d = djinni.item_to_draft(items[1])
    assert d.external_id == "852172" and d.title == "Python Developer (microservices)"
    assert d.url == "https://djinni.co/jobs/852172-python-developer-microservices/"
    assert d.skills == ["Python"]  # empty <category/> dropped
    assert d.published_at.day == 7 and "Dataforest" in d.description and "<p>" not in d.description
    src = djinni.DjinniSource(cfg())
    assert src.external_id_from_url(d.url) == "852172"


async def test_djinni_search_passes_keyword_and_filters(monkeypatch):
    xml = load_text("djinni.xml")
    calls = []

    async def fake_feed(url, params=None, **kw):
        calls.append(params)
        return xml

    monkeypatch.setattr(djinni, "fetch_feed", fake_feed)
    src = djinni.DjinniSource(cfg())
    drafts = await src.search(q("Python microservices"), 10)
    assert calls[-1] == {"primary_keyword": "Python"}
    assert drafts[0].external_id == "852172"  # title matches first
    assert len(await src.search(q("python"), 2)) == 2
    assert await src.search(q("kubernetes"), 10) == []
    await src.search(q("бэкенд"), 10)
    assert calls[-1] is None  # unknown query -> general feed


# --- getmatch -------------------------------------------------------------------------------

def test_getmatch_fixture_to_drafts():
    offers = {o["id"]: o for o in load_json("getmatch.json")["offers"]}
    hidden = getmatch.item_to_draft(offers[36009])
    assert hidden.title == "Senior Python Developer" and hidden.company == "TradingView"
    assert hidden.salary_from is None and hidden.currency is None and hidden.salary_gross is None
    assert hidden.remote is False and hidden.location == "Tbilisi (Georgia) (гибрид); Limassol (Cyprus) (гибрид)"
    assert hidden.url == "https://getmatch.ru/vacancies/36009-senior-python-developer"
    assert hidden.description.startswith("Английский: B2 — Upper-Intermediate")
    assert "Python" in hidden.skills and hidden.is_partial

    eur = getmatch.item_to_draft(offers[36671])
    assert (eur.salary_from, eur.salary_to, eur.currency, eur.salary_gross) == (4000, 5000, "EUR", False)
    assert eur.remote is True

    rub = getmatch.item_to_draft(offers[36670])
    assert (rub.salary_from, rub.salary_to, rub.currency, rub.salary_gross) == (250000, 450000, "RUR", True)
    assert rub.published_at.year == 2026


def test_getmatch_full_offer_not_partial():
    offer = copy.deepcopy(load_json("getmatch.json")["offers"][0])
    offer["description"] = "<p>Полное описание вакансии</p>"
    d = getmatch.item_to_draft(offer)
    assert not d.is_partial and d.description.endswith("Полное описание вакансии")


async def test_getmatch_search_filters_and_limits(monkeypatch):
    data = load_json("getmatch.json")
    data["offers"].append({"id": 1, "position": "Python", "skills_objects": 5})  # malformed
    calls = []

    async def fake_fetch(url, params=None, **kw):
        calls.append(params)
        return data

    monkeypatch.setattr(getmatch, "fetch_json", fake_fetch)
    src = getmatch.GetmatchSource(cfg())
    drafts = await src.search(q("devops"), 10)
    assert [d.external_id for d in drafts] == ["36671"]
    assert calls[-1]["q"] == "devops" and calls[-1]["limit"] == 20
    assert len(await src.search(q("python"), 1)) == 1
    assert "1" not in [d.external_id for d in await src.search(q("python"), 10)]
    assert src.external_id_from_url("https://getmatch.ru/vacancies/36671-senior-devops-engineer") == "36671"


async def test_getmatch_unexpected_response(monkeypatch):
    async def fake_fetch(*_a, **_k):
        return {"error": "x"}

    monkeypatch.setattr(getmatch, "fetch_json", fake_fetch)
    with pytest.raises(SourceError):
        await getmatch.GetmatchSource(cfg()).search(q("python"), 10)
