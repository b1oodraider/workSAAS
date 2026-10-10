"""Source hardening: response size cap, per-host circuit breaker, safe fetcher defaults,
per-run query budget and memoisation, local ranking, salary/currency parsing, option parsing,
timezones and trust flags. No network: httpx gets a MockTransport, fetchers are faked."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from app.core.config import SourceConfig
from app.sources import (
    adzuna,
    arbeitnow,
    ashby,
    avito_jobs,
    boards,
    djinni,
    geekjob,
    greenhouse,
    himalayas,
    jobicy,
    lever,
    linkedin,
    rabota_ru,
    remote_job_ru,
    remotive,
    telegram_channels,
    themuse,
    web,
    weworkremotely,
    workingnomads,
    yandex_jobs,
)
from app.sources.base import JobSource, SearchQuery, SourceError, VacancyDraft, relevance_key
from app.sources.hh_web import _parse_dt as hh_parse_dt
from app.sources.jsonld import parse_date
from app.sources.rss import _dt as rss_dt
from app.sources.salary import detect_currency, known_currency, parse_salary_text
from app.sources.web import Page, breaker, response_cache

_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def clean_web_state(monkeypatch):
    """The breaker and the response cache are process-wide: isolate every test."""
    async def no_wait(_host, _interval):
        return None

    monkeypatch.setattr(web.rate_limiter, "wait", no_wait)
    breaker.clear()
    response_cache.clear()
    yield
    breaker.clear()
    response_cache.clear()


def q(text: str) -> SearchQuery:
    return SearchQuery(text=text)


def mock_httpx(monkeypatch, handler) -> list[httpx.Request]:
    """Route every httpx.AsyncClient created by app.sources.web through ``handler``."""
    calls: list[httpx.Request] = []
    real = _REAL_ASYNC_CLIENT

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    def factory(*args, **kwargs):
        kwargs.pop("proxy", None)
        return real(*args, transport=httpx.MockTransport(wrapped), **kwargs)

    monkeypatch.setattr(web.httpx, "AsyncClient", factory)
    return calls


class RepeatFetcher:
    """Fake PageFetcher that answers every request with the same page."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.calls: list[tuple[str, dict | None]] = []

    async def get(self, url, params=None):
        self.calls.append((url, params))
        return self.page

    async def close(self):
        pass


# --- 1. response size cap ------------------------------------------------------------------

async def test_http_fetcher_rejects_oversized_body(monkeypatch):
    monkeypatch.setattr(web, "MAX_RESPONSE_BYTES", 1000)
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, content=b"x" * 5000))
    fetcher = web.HttpFetcher(min_interval=0)
    try:
        with pytest.raises(SourceError, match="ответ слишком большой"):
            await fetcher.get("https://big.example/page")
    finally:
        await fetcher.close()


async def test_http_fetcher_reads_body_under_cap(monkeypatch):
    monkeypatch.setattr(web, "MAX_RESPONSE_BYTES", 1000)
    mock_httpx(monkeypatch, lambda r: httpx.Response(
        200, content="<html>привет</html>".encode(), headers={"content-type": "text/html; charset=utf-8"}))
    fetcher = web.HttpFetcher(min_interval=0)
    try:
        page = await fetcher.get("https://ok.example/page")
    finally:
        await fetcher.close()
    assert page.status == 200 and page.text == "<html>привет</html>"


async def test_fetch_json_rejects_oversized_body_and_declared_length(monkeypatch):
    monkeypatch.setattr(web, "MAX_RESPONSE_BYTES", 100)
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, content=b"[" + b"1," * 200 + b"1]"))
    with pytest.raises(SourceError, match="ответ слишком большой"):
        await web.fetch_json("https://api.example/jobs", min_interval=0)
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, content=b"[]", headers={"content-length": "999999"}))
    with pytest.raises(SourceError, match="ответ слишком большой"):
        await web.fetch_json("https://api.example/jobs", min_interval=0)


async def test_fetch_json_parses_small_body(monkeypatch):
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"jobs": [1, 2]}))
    assert await web.fetch_json("https://api.example/jobs", min_interval=0) == {"jobs": [1, 2]}


# --- 2. circuit breaker --------------------------------------------------------------------

async def test_http_429_opens_breaker_without_further_network(monkeypatch):
    calls = mock_httpx(monkeypatch, lambda r: httpx.Response(429, text="slow down"))
    fetcher = web.HttpFetcher(min_interval=0)
    try:
        page = await fetcher.get("https://www.ratelimited.example/search")
        assert page.status == 429  # the source still sees the page and reports it itself
        with pytest.raises(SourceError, match=r"ratelimited\.example временно ограничил доступ — пауза ещё ~\d+ мин"):
            await fetcher.get("https://www.ratelimited.example/other")
    finally:
        await fetcher.close()
    assert len(calls) == 1


async def test_captcha_page_opens_breaker(monkeypatch):
    calls = mock_httpx(monkeypatch, lambda r: httpx.Response(200, text="<html>Please solve the captcha</html>"))
    fetcher = web.HttpFetcher(min_interval=0, cache_ttl=3600)
    try:
        assert (await fetcher.get("https://captcha.example/")).looks_blocked
        with pytest.raises(SourceError, match="временно ограничил доступ"):
            await fetcher.get("https://captcha.example/")
    finally:
        await fetcher.close()
    assert len(calls) == 1
    breaker.clear()
    # A blocked page is never cached as if it were a real answer.
    fetcher = web.HttpFetcher(min_interval=0, cache_ttl=3600)
    try:
        await fetcher.get("https://captcha.example/")
    finally:
        await fetcher.close()
    assert len(calls) == 2


async def test_fetch_json_439_opens_breaker(monkeypatch):
    calls = mock_httpx(monkeypatch, lambda r: httpx.Response(439))
    with pytest.raises(SourceError, match="ограничил частоту"):
        await web.fetch_json("https://api.blocked.example/x", min_interval=0)
    with pytest.raises(SourceError, match="временно ограничил доступ"):
        await web.fetch_json("https://api.blocked.example/y", min_interval=0)
    assert len(calls) == 1
    # Other hosts are not affected.
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json=[]))
    assert await web.fetch_json("https://api.fine.example/x", min_interval=0) == []


async def test_plain_errors_do_not_open_breaker(monkeypatch):
    calls = mock_httpx(monkeypatch, lambda r: httpx.Response(404))
    for _ in range(2):
        with pytest.raises(SourceError, match="ответил 404"):
            await web.fetch_json("https://api.missing.example/x", min_interval=0)
    assert len(calls) == 2 and not breaker.blocked("api.missing.example")


def test_breaker_backoff_doubles_up_to_six_hours_and_resets(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(web.time, "time", lambda: now[0])
    host = "slow.example"
    pauses = []
    for _ in range(5):
        breaker.trip(host)
        pauses.append(breaker._until[host] - now[0])
        now[0] += pauses[-1] + 1  # the next block comes after this pause ended
    assert pauses == [3600, 7200, 14400, 21600, 21600]
    # the last pause is over too
    breaker.check(host)  # does not raise
    breaker.ok(host)
    breaker.trip(host)
    assert breaker._until[host] - now[0] == 3600


async def test_browser_fetcher_respects_breaker_before_launching(monkeypatch):
    breaker.trip("www.avito.ru")
    fetcher = web.BrowserFetcher()

    async def must_not_launch():
        raise AssertionError("browser launched for a blocked host")

    monkeypatch.setattr(fetcher, "_ensure", must_not_launch)
    with pytest.raises(SourceError, match="avito.ru временно ограничил доступ"):
        await fetcher.get("https://www.avito.ru/all/vakansii", {"q": "python"})


async def test_auto_fetcher_trips_breaker_only_when_browser_cannot_help(monkeypatch):
    mock_httpx(monkeypatch, lambda r: httpx.Response(403, text="Access denied"))
    monkeypatch.setattr(web.BrowserFetcher, "available", staticmethod(lambda: True))
    auto = web.AutoFetcher(min_interval=0)
    browser_calls = []

    async def fake_browser_get(url, params=None):
        browser_calls.append(url)
        return Page(url, 200, "<html>real page</html>" + "x" * 30000)

    monkeypatch.setattr(auto.browser, "get", fake_browser_get)
    try:
        page = await auto.get("https://auto.example/search")
    finally:
        await auto.http.close()
    assert page.status == 200 and browser_calls and not breaker.blocked("auto.example")

    monkeypatch.setattr(web.BrowserFetcher, "available", staticmethod(lambda: False))
    auto = web.AutoFetcher(min_interval=0)
    try:
        assert (await auto.get("https://noauto.example/search")).status == 403
    finally:
        await auto.http.close()
    assert breaker.blocked("noauto.example")


async def test_boards_fetch_feed_respects_breaker(monkeypatch):
    calls = mock_httpx(monkeypatch, lambda r: httpx.Response(200, text="<rss/>"))
    breaker.trip("weworkremotely.com")
    with pytest.raises(SourceError, match="временно ограничил доступ"):
        await boards.fetch_feed("https://weworkremotely.com/categories/x.rss", source="WWR")
    assert calls == []


# --- 3/4. safe fetcher defaults and caching of search pages ---------------------------------

@pytest.mark.parametrize("cls", [rabota_ru.RabotaRuSource, remote_job_ru.RemoteJobRuSource, geekjob.GeekjobSource,
                                 linkedin.LinkedInSource, avito_jobs.AvitoJobsSource])
async def test_scrapers_default_to_plain_http_with_hour_cache(cls):
    fetcher = cls(SourceConfig())._fetcher()
    try:
        assert isinstance(fetcher, web.HttpFetcher) and fetcher.cache_ttl == 3600
    finally:
        await fetcher.close()
    auto = cls(SourceConfig(options={"fetcher": "auto"}))._fetcher()
    try:
        assert isinstance(auto, web.AutoFetcher)  # explicit opt-in still works
    finally:
        await auto.close()


async def test_unknown_fetcher_mode_falls_back_to_http():
    fetcher = web.make_fetcher("brwoser")
    try:
        assert isinstance(fetcher, web.HttpFetcher)
    finally:
        await fetcher.close()


async def test_geekjob_search_is_cached(monkeypatch):
    seen_kwargs = []

    async def fake_fetch_json(url, **kwargs):
        seen_kwargs.append(kwargs)
        return {"data": [], "nextpage": 0}

    monkeypatch.setattr(geekjob, "fetch_json", fake_fetch_json)
    await geekjob.GeekjobSource(SourceConfig()).search(q("python"), 10)
    assert seen_kwargs[0]["cache_ttl"] == 3600 and seen_kwargs[0]["min_interval"] >= 2.0


# --- 5. per-run query budget ----------------------------------------------------------------

async def test_linkedin_only_first_queries_hit_network(monkeypatch):
    src = linkedin.LinkedInSource(SourceConfig())
    fetcher = RepeatFetcher(Page(linkedin.SEARCH_URL, 200, "<html></html>"))
    monkeypatch.setattr(src, "_fetcher", lambda: fetcher)
    for text in ["a1", "a2", "a3", "a4", "a5"]:
        assert await src.search(q(text), 10) == []
    assert [c[1]["keywords"] for c in fetcher.calls] == ["a1", "a2", "a3"]


async def test_avito_query_budget_option(monkeypatch):
    src = avito_jobs.AvitoJobsSource(SourceConfig(options={"max_queries": 1}))
    fetcher = RepeatFetcher(Page("https://www.avito.ru/all/vakansii", 200, "<html></html>"))
    monkeypatch.setattr(src, "_fetcher", lambda: fetcher)
    await src.search(q("python"), 10)
    await src.search(q("java"), 10)
    assert [c[1]["q"] for c in fetcher.calls] == ["python"]


async def test_telegram_reads_each_channel_once_per_run(monkeypatch):
    src = telegram_channels.TelegramChannelsSource(SourceConfig(options={"channels": ["chan_a", "@chan_b", "../x"],
                                                                         "pages": 100}))
    reads = []
    post = VacancyDraft(source="telegram", external_id="chan_a/1", title="Python developer",
                        description="Ищем Python разработчика")

    async def fake_read(_fetcher, channel, pages):
        reads.append((channel, pages))
        return [post] if channel == "chan_a" else []

    monkeypatch.setattr(src, "_read_channel", fake_read)
    for text in ["python", "golang", "python developer", "java", "python"]:
        await src.search(q(text), 10)
    assert reads == [("chan_a", 5), ("chan_b", 5)]  # invalid name dropped, pages capped at 5
    assert [d.external_id for d in await src.search(q("python"), 10)] == ["chan_a/1"]


# --- 6. memoised boards/feeds and negative cache --------------------------------------------

async def test_greenhouse_boards_fetched_once_and_failures_not_retried(monkeypatch):
    calls = []

    async def fake_fetch_json(url, **_kw):
        calls.append(url)
        if "/dead/" in url:
            raise SourceError("Greenhouse dead ответил 404")
        return {"jobs": [{"id": 1, "title": "Python Engineer", "absolute_url": "https://x", "content": ""}]}

    monkeypatch.setattr(greenhouse, "fetch_json", fake_fetch_json)
    src = greenhouse.GreenhouseSource(SourceConfig(options={"companies": ["alive", "dead"]}))
    for text in ["python", "engineer", "golang"]:
        await src.search(q(text), 10)
    assert sorted(calls) == [f"{greenhouse.API}/alive/jobs", f"{greenhouse.API}/dead/jobs"]


async def test_board_sources_share_base_class():
    for cls in (greenhouse.GreenhouseSource, lever.LeverSource, ashby.AshbySource):
        assert issubclass(cls, boards.BoardSource) and cls.default_companies


async def test_whole_feed_sources_fetch_once_per_run(monkeypatch):
    counts = {"arbeitnow": 0, "workingnomads": 0, "themuse": 0}
    item = {"slug": "py", "title": "Python Developer", "tags": [], "created_at": 1791000000,
            "url": "https://www.workingnomads.com/job/go/1/", "id": 7, "name": "Python Developer", "contents": ""}

    def fake(name, payload):
        async def _fetch(_url, **_kw):
            counts[name] += 1
            return payload
        return _fetch

    monkeypatch.setattr(arbeitnow, "fetch_json", fake("arbeitnow", {"data": [item], "links": {}}))
    monkeypatch.setattr(workingnomads, "fetch_json", fake("workingnomads", [item]))
    monkeypatch.setattr(themuse, "fetch_json", fake("themuse", {"results": [item], "page_count": 1}))
    sources = [arbeitnow.ArbeitnowSource(SourceConfig()), workingnomads.WorkingNomadsSource(SourceConfig()),
               themuse.TheMuseSource(SourceConfig(options={"categories": ["Software Engineering"]}))]
    for src in sources:
        for text in ["python", "developer", "golang"]:
            await src.search(q(text), 10)
        assert len(await src.search(q("python"), 10)) == 1
    assert counts == {"arbeitnow": 1, "workingnomads": 1, "themuse": 1}


async def test_rss_board_sources_parse_feeds_once(monkeypatch):
    feeds = []

    async def fake_feed(url, **_kw):
        feeds.append(url)
        return "<rss><channel></channel></rss>"

    monkeypatch.setattr(weworkremotely, "fetch_feed", fake_feed)
    monkeypatch.setattr(djinni, "fetch_feed", fake_feed)
    wwr = weworkremotely.WeWorkRemotelySource(SourceConfig(options={"feeds": "https://weworkremotely.com/a.rss"}))
    dj = djinni.DjinniSource(SourceConfig())
    for text in ["python", "python developer", "django"]:
        await wwr.search(q(text), 10)
        await dj.search(q(text), 10)  # all three map to the "Python" keyword
    assert feeds == ["https://weworkremotely.com/a.rss", djinni.FEED]


# --- 7. local ranking -----------------------------------------------------------------------

def _draft(i: str, title: str, day: int, desc: str = "") -> VacancyDraft:
    return VacancyDraft(source="x", external_id=i, title=title, description=desc,
                        published_at=datetime(2026, 10, day))


def test_pick_ranks_by_number_of_title_words_then_body_then_date():
    drafts = [
        _draft("java", "Java Developer", 9),
        _draft("py", "Python Developer", 1),
        _draft("body", "Backend Engineer", 8, "python developer wanted"),
        _draft("body1", "Backend Engineer", 9, "developer"),
    ]
    found = boards.pick(drafts, q("python developer"), 10)
    assert [d.external_id for d in found] == ["py", "java", "body", "body1"]


def test_relevance_key_counts_distinct_words():
    words = ["python", "developer"]
    assert relevance_key("Python Developer", "", None, words) > relevance_key("Java Developer", "python", None, words)


# --- 8. salary text -------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("3000-5000 USD", (3000, 5000, "USD", None)),
    ("200-300 тыс. руб.", (200000, 300000, "RUR", None)),
    ("brutto 4000 EUR", (4000, None, "EUR", True)),
    ("от 150 000 ₽", (150000, None, "RUR", None)),
    ("150k-200k $", (150000, 200000, "USD", None)),
    ("150 т.р.", (150000, None, "RUR", None)),
    ("$4,000/month", (4000, None, "USD", None)),
    ("3 000 BYN", (3000, None, "BYR", None)),
    ("2 000 евро", (2000, None, "EUR", None)),
    ("от 500 000 тенге", (500000, None, "KZT", None)),
    ("£60,000", (60000, None, "GBP", None)),
    ("Доход 100 000 - 150 000", (100000, 150000, None, None)),  # "до" inside a word is not "up to"
    ("до 3 000 $", (None, 3000, "USD", None)),
    ("1 500 долларов", (1500, None, "USD", None)),
])
def test_parse_salary_text_currencies_and_thousands(text, expected):
    assert parse_salary_text(text) == expected


def test_currency_helpers():
    assert detect_currency("brutto, bring your laptop") is None
    assert detect_currency("5000 usd (≈ 450 000 руб.)") == "USD"  # the first one mentioned
    assert known_currency("rub") == "RUR" and known_currency(" usd ") == "USD"
    assert known_currency("XYZ") is None and known_currency(None) is None


# --- 9. international salaries need a known currency ----------------------------------------

def test_lever_salary_float_strings_and_unknown_currency():
    base = {"id": "a" * 8 + "-0000-0000-0000-" + "0" * 12, "text": "Engineer", "categories": {}}
    d = lever.item_to_draft({**base, "salaryRange": {"min": "100000.0", "max": 150000.5, "currency": "USD",
                                                     "interval": "per-year-salary"}}, "acme")
    assert (d.salary_from, d.salary_to, d.currency) == (100000, 150000, "USD")
    d = lever.item_to_draft({**base, "salaryRange": {"min": 100000, "max": 150000,
                                                     "interval": "per-year-salary"}}, "acme")
    assert (d.salary_from, d.salary_to, d.currency) == (None, None, None)
    assert "100000–150000" in d.description


def test_ashby_salary_float_strings_and_unknown_currency():
    def item(code):
        comp = {"summaryComponents": [{"compensationType": "Salary", "interval": "1 YEAR",
                                       "minValue": "120000.0", "maxValue": "160000.0", "currencyCode": code}],
                "compensationTierSummary": "$120K – $160K"}
        return {"id": "j1", "title": "Engineer", "compensation": comp}
    d = ashby.item_to_draft(item("USD"), "acme")
    assert (d.salary_from, d.salary_to, d.currency) == (120000, 160000, "USD")
    d = ashby.item_to_draft(item(None), "acme")
    assert (d.salary_from, d.salary_to, d.currency) == (None, None, None)
    assert "$120K" in d.description


def test_feeds_without_currency_keep_salary_as_text():
    h = himalayas.item_to_draft({"guid": "https://himalayas.app/companies/a/jobs/x", "title": "Dev",
                                 "minSalary": 50000, "maxSalary": 70000})
    assert h.salary_from is None and h.currency is None and "50000–70000" in h.description
    h = himalayas.item_to_draft({"guid": "https://himalayas.app/companies/a/jobs/x", "title": "Dev",
                                 "minSalary": "50000.0", "currency": "eur"})
    assert (h.salary_from, h.currency) == (50000, "EUR")
    j = jobicy.item_to_draft({"id": 1, "jobTitle": "Dev", "salaryMin": 60000})
    assert j.salary_from is None and "60000" in j.description
    r = remotive.item_to_draft({"id": 1, "title": "Dev", "salary": "50k-70k"})
    assert r.salary_from is None and "50k-70k" in r.description
    r = remotive.item_to_draft({"id": 1, "title": "Dev", "salary": "$50k-$70k"})
    assert (r.salary_from, r.salary_to, r.currency) == (50000, 70000, "USD")
    a = adzuna.item_to_draft({"id": 1, "title": "Dev", "salary_min": 40000}, "xx")
    assert a.salary_from is None and a.currency is None and "40000" in a.description
    assert adzuna.item_to_draft({"id": 1, "title": "Dev", "salary_min": 40000}, "gb").currency == "GBP"


# --- 10. option parsing ---------------------------------------------------------------------

class _Opts(JobSource):
    name = "opts"

    async def search(self, query, limit):
        return []


def test_option_helpers_never_raise():
    src = _Opts(SourceConfig(options={"pages": "lots", "big": 99, "num": "3", "flt": "nan", "slow": 0.1,
                                      "lst": "a, b ,", "bad": 5, "empty": [], "boolean": True}))
    assert src.opt_int("pages", 2, lo=1, hi=5) == 2
    assert src.opt_int("big", 2, lo=1, hi=5) == 5 and src.opt_int("num", 1) == 3
    assert src.opt_int("boolean", 2) == 2 and src.opt_int("missing", 4) == 4
    assert src.opt_float("flt", 2.0) == 2.0 and src.opt_float("slow", 2.0, floor=1.0) == 1.0
    assert src.opt_float("pages", 1.5) == 1.5
    assert src.opt_list("lst") == ["a", "b"] and src.opt_list("bad", ["d"]) == ["d"]
    assert src.opt_list("empty", ["d"]) == [] and src.opt_list("missing", ("x",)) == ["x"]


async def test_sources_survive_bad_numeric_options(monkeypatch):
    calls = []

    async def fake_fetch_json(url, *, params=None, min_interval=0.0, **_kw):
        calls.append(min_interval)
        return {"results": []}

    monkeypatch.setattr(yandex_jobs, "fetch_json", fake_fetch_json)
    src = yandex_jobs.YandexJobsSource(SourceConfig(options={"pages": "many", "min_interval": "0"}))
    assert await src.search(q("python"), 10) == []
    assert calls == [1.0]  # min_interval floor
    bad = arbeitnow.ArbeitnowSource(SourceConfig(options={"pages": None}))
    assert bad.opt_int("pages", 2, lo=1, hi=5) == 2
    assert linkedin.LinkedInSource(SourceConfig(options={"locations": 42}))._locations() == ["Worldwide"]
    assert adzuna.AdzunaSource(SourceConfig(options={"countries": "gb, DE, nope"}))._countries() == ["gb", "de"]
    assert boards.company_tokens({"companies": 42}) == []


# --- 11. timezones --------------------------------------------------------------------------

def test_dates_are_converted_to_naive_utc():
    assert parse_date("2026-10-08T10:00:00+03:00") == datetime(2026, 10, 8, 7, 0)
    assert parse_date("2026-10-08T01:00:00-05:00") == datetime(2026, 10, 8, 6, 0)
    assert parse_date("2026-10-08T10:00:00Z") == datetime(2026, 10, 8, 10, 0)
    assert parse_date("2026-10-08") == datetime(2026, 10, 8)
    assert rss_dt("Thu, 08 Oct 2026 10:00:00 +0300") == datetime(2026, 10, 8, 7, 0)
    assert hh_parse_dt("2026-10-08T10:00:00+0300") == datetime(2026, 10, 8, 7, 0)


# --- 12. trust flags ------------------------------------------------------------------------

def test_open_posting_sources_are_untrusted():
    assert linkedin.LinkedInSource.trusted is False
    assert adzuna.AdzunaSource.trusted is False


@pytest.mark.parametrize("text,expected", [
    ("от 150 000 ₽ до 200 000 ₽, бонус 50к", (150000, 200000, "RUR")),
    ("от 100 тыс. до 150 000 руб", (100000, 150000, "RUR")),
    ("Удалённо из Европы, 3000-4000$", (3000, 4000, "USD")),
    ("Remote (Европа) 4000-5000 USD", (4000, 5000, "USD")),
    ("3000-4000 евро", (3000, 4000, "EUR")),
])
def test_salary_mixed_shorthand_and_europe(text, expected):
    from app.sources.salary import parse_salary_text

    assert parse_salary_text(text)[:3] == expected


def test_breaker_does_not_stack_and_honours_retry_after():
    from app.sources.web import breaker

    breaker.clear()
    breaker.trip("api.example.com")
    first = breaker._until["api.example.com"]
    breaker.trip("api.example.com")  # a second request that was already in flight
    assert breaker._until["api.example.com"] == first
    breaker.clear()
    breaker.trip("api.example.com", seconds=30)  # Retry-After: 30 -> a minute, not an hour
    assert breaker._until["api.example.com"] - __import__("time").time() <= 61
    breaker.clear()


@pytest.mark.parametrize("page,blocks", [
    ({"url": "https://hh.ru/search/vacancy?text=captcha+qa", "status": 200, "text": "x" * 300_000}, False),
    ({"url": "https://t.me/s/pydevjob", "status": 503, "text": "Service Unavailable"}, False),
    ({"url": "https://example.com/jobs", "status": 403, "text": "Access denied"}, True),
    ({"url": "https://hh.ru/account/captcha?backurl=/", "status": 200, "text": "..."}, True),
    ({"url": "https://example.com/jobs", "status": 429, "text": ""}, True),
])
def test_only_real_blocks_pause_a_host(page, blocks):
    from app.sources.web import Page, _is_block

    assert _is_block(Page(**page)) is blocks


def test_naive_utc_survives_out_of_range_offsets():
    from datetime import datetime, timedelta, timezone

    from app.sources.base import naive_utc

    dt = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=3)))
    assert naive_utc(dt) == datetime(1, 1, 1)


@pytest.mark.parametrize("value,expected", [
    (150_000, 150_000), ("250000", 250_000), (999_999_999, 999_999_999), (None, None),
    (10**20, None), ("99999999999999999999", None), (1e30, None), ("1e30", None), (10**400, None),
    (float("inf"), None), (float("nan"), None), (0, None), (-5000, None), (10**9, None),
])
def test_draft_salary_out_of_range_is_no_salary(value, expected):
    d = VacancyDraft(source="x", external_id="1", title="t", salary_from=value, salary_to=value)
    assert d.salary_from == expected and d.salary_to == expected
    assert d.updated(salary_from=value).salary_from == expected  # validated copy, unlike model_copy


def test_draft_salary_garbage_is_still_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VacancyDraft(source="x", external_id="1", title="t", salary_from="договорная")


def test_huge_salary_in_untrusted_text_does_not_reach_the_db():
    from app.sources.jsonld import draft_from_job_posting

    post = "Python-разработчик\nЗП 99999999999999999999 руб\nПишите @hr"
    d = telegram_channels.post_to_draft("chan", "1", post, None)
    assert d.salary_from is None and d.salary_to is None
    jp = {"title": "Python dev", "baseSalary": {"currency": "RUB", "value": {"minValue": "1e30", "maxValue": 1e300}}}
    d = draft_from_job_posting(jp, source="manual", external_id="1", url=None)
    assert d.salary_from is None and d.salary_to is None
    jp = {"url": "https://www.rabota.ru/vacancy/123/", "title": "Python dev",
          "baseSalary": {"currency": "RUB", "minValue": 10**25}}
    assert rabota_ru.posting_to_draft(jp).salary_from is None


@pytest.mark.parametrize("options,probed", [({}, True), ({"channels": ["chan_a"]}, True), ({"channels": []}, False)])
async def test_doctor_probes_telegram_with_default_channels(options, probed, env, monkeypatch):
    from app import doctor
    from app.sources import registry

    async def fake_probe(url, *, use_proxy, headers=None):
        return doctor.Check(url, True)

    monkeypatch.setattr(doctor, "_probe", fake_probe)
    for name in registry.SOURCE_CLASSES:
        monkeypatch.setitem(env.sources, name, SourceConfig(enabled=name == "telegram", options=options))
    checks = await doctor.check_sources()
    assert bool(checks) is probed
