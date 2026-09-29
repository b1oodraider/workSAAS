"""hh.ru website parsers. Fixtures mimic hh.ru markup as known at the time of writing;
real pages could not be fetched from the development environment, so these tests pin
the parsing strategies (embedded state, data-qa markup, JSON-LD), not the live site."""

from __future__ import annotations

import json

import pytest

from app.core.config import SourceConfig
from app.sources import hh as hh_module
from app.sources.base import SearchQuery, SourceError
from app.sources.hh import ApiDenied, HHSource
from app.sources.hh_web import parse_salary_text, parse_search_page, parse_vacancy_page
from app.sources.web import Page

STATE_PAGE = """<html><body><template id="HH-Lux-InitialState">{}</template></body></html>""".format(
    json.dumps({"vacancySearchResult": {"vacancies": [
        {"vacancyId": 111, "name": "Python-разработчик", "company": {"visibleName": "Acme"},
         "area": {"name": "Москва"}, "compensation": {"from": 200000, "to": 300000,
                                                      "currencyCode": "RUR", "gross": False},
         "snippet": {"req": "Опыт <highlighttext>Python</highlighttext> от 3 лет", "resp": "Писать API"},
         "links": {"desktop": "https://hh.ru/vacancy/111"},
         "publicationTime": {"$": "2026-09-20T10:00:00+0300"}},
        {"vacancyId": 222, "name": "Go developer", "company": {"name": "Beta"}},
    ]}}, ensure_ascii=False))

MARKUP_PAGE = """
<div data-qa="vacancy-serp__vacancy vacancy-serp__vacancy_standard">
  <h2><a data-qa="serp-item__title" href="https://hh.ru/vacancy/333?query=python">
    <span data-qa="serp-item__title-text">Backend-разработчик (Python)</span></a></h2>
  <span>от 250 000 ₽ за месяц, на руки</span>
  <a data-qa="vacancy-serp__vacancy-employer"><span data-qa="vacancy-serp__vacancy-employer-text">ООО Ромашка</span></a>
  <span data-qa="vacancy-serp__vacancy-address">Санкт-Петербург</span>
  <span data-qa="vacancy-label-remote-work-schedule">Можно удалённо</span>
  <div data-qa="vacancy-serp__vacancy_snippet_requirement">FastAPI, PostgreSQL</div>
</div>
<a data-qa="pager-next" href="?page=1">дальше</a>
"""

LINKS_PAGE = '<a href="/vacancy/444">Data Engineer</a> <a href="/vacancy/444">Data Engineer</a> <a href="/vacancy/555">x</a>'

VACANCY_PAGE = """<html><head><script type="application/ld+json">{}</script></head>
<body><div data-qa="vacancy-salary">от 200 000 до 300 000 ₽ до вычета налогов</div>
<span data-qa="skills-element">Python</span><span data-qa="skills-element">SQL</span>
<span data-qa="vacancy-experience">3–6 лет</span></body></html>""".format(json.dumps({
    "@context": "https://schema.org", "@type": "JobPosting", "title": "Senior Python",
    "description": "<p>Делаем платежи</p><ul><li>Python</li></ul>", "datePosted": "2026-09-21",
    "hiringOrganization": {"@type": "Organization", "name": "Acme"},
    "jobLocation": {"@type": "Place", "address": {"addressLocality": "Москва"}},
    "baseSalary": {"currency": "RUB", "value": {"minValue": 200000, "maxValue": 300000}},
    "jobLocationType": "TELECOMMUTE",
}, ensure_ascii=False))

MARKUP_VACANCY_PAGE = """<h1 data-qa="vacancy-title">Аналитик</h1>
<a data-qa="vacancy-company-name">Бета</a><div data-qa="vacancy-description"><p>Анализ данных</p></div>"""


def test_search_page_embedded_state():
    drafts = parse_search_page(STATE_PAGE)
    assert [d.external_id for d in drafts] == ["111", "222"]
    d = drafts[0]
    assert d.company == "Acme" and d.salary_from == 200000 and d.currency == "RUR"
    assert "Опыт Python от 3 лет" in d.description and d.is_partial
    assert d.published_at.day == 20


def test_search_page_markup_fallback():
    drafts = parse_search_page(MARKUP_PAGE)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.external_id == "333" and d.title == "Backend-разработчик (Python)"
    assert d.company == "ООО Ромашка" and d.location == "Санкт-Петербург" and d.remote
    assert (d.salary_from, d.salary_to, d.currency, d.salary_gross) == (250000, None, "RUR", False)


def test_search_page_links_fallback_dedups_and_skips_short_titles():
    drafts = parse_search_page(LINKS_PAGE)
    assert [(d.external_id, d.title) for d in drafts] == [("444", "Data Engineer")]


def test_search_page_garbage_does_not_crash():
    assert parse_search_page("<html><template id='HH-Lux-InitialState'>{broken</template></html>") == []


def test_vacancy_page_json_ld():
    d = parse_vacancy_page(VACANCY_PAGE, "777")
    assert d.title == "Senior Python" and d.company == "Acme" and d.location == "Москва"
    assert (d.salary_from, d.salary_to, d.currency, d.salary_gross) == (200000, 300000, "RUR", True)
    assert d.remote and d.skills == ["Python", "SQL"] and "• Python" in d.description
    assert not d.is_partial and d.experience == "3–6 лет"


def test_vacancy_page_markup_fallback():
    d = parse_vacancy_page(MARKUP_VACANCY_PAGE, "888")
    assert d.title == "Аналитик" and d.company == "Бета" and "Анализ данных" in d.description
    assert parse_vacancy_page("<html></html>", "1") is None


@pytest.mark.parametrize("text,expected", [
    ("от 150 000 до 250 000 ₽ за месяц, на руки", (150000, 250000, "RUR", False)),
    ("до 3 000 $", (None, 3000, "USD", None)),
    ("от 100 000 ₽ до вычета налогов", (100000, None, "RUR", True)),
    ("", (None, None, None, None)),
])
def test_parse_salary_text(text, expected):
    assert parse_salary_text(text) == expected


class FakeFetcher:
    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    async def get(self, url, params=None):
        self.urls.append((url, params))
        return self.pages.pop(0)

    async def close(self):
        pass


async def test_auto_mode_falls_back_to_web_when_api_denied(monkeypatch):
    monkeypatch.setattr(hh_module, "_api_denied_until", 0.0)
    src = HHSource(SourceConfig(options={"mode": "auto", "area": 1}))

    async def denied(*_a, **_k):
        raise ApiDenied("403")

    monkeypatch.setattr(src, "_api_search", denied)
    fetcher = FakeFetcher([Page("https://hh.ru/search/vacancy", 200, MARKUP_PAGE),
                           Page("https://hh.ru/search/vacancy", 200, MARKUP_PAGE)])
    monkeypatch.setattr(src, "_fetcher", lambda: fetcher)
    drafts = await src.search(SearchQuery(text="python"), limit=10)
    assert [d.external_id for d in drafts] == ["333"]
    url, params = fetcher.urls[0]
    assert url.endswith("/search/vacancy") and params["text"] == "python" and params["area"] == "1"
    assert params["search_period"] == 30  # 14 days -> nearest allowed period
    assert not src._use_api()  # API skipped for a while


async def test_web_search_reports_captcha(monkeypatch):
    src = HHSource(SourceConfig(options={"mode": "web"}))
    fetcher = FakeFetcher([Page("https://hh.ru/account/captcha", 200, "<html>captcha</html>")])
    monkeypatch.setattr(src, "_fetcher", lambda: fetcher)
    with pytest.raises(SourceError, match="капчу"):
        await src.search(SearchQuery(text="python"), limit=10)


@pytest.mark.parametrize("text,expected", [
    ("ЗП: 200-300k", (200000, 300000, None, None)),
    ("вилка 250к на руки", (250000, None, None, False)),
    ("150 000 ₽ до вычета налогов", (150000, None, "RUR", True)),
    ("$3.5k", (3500, None, "USD", None)),
])
def test_parse_salary_shorthand(text, expected):
    assert parse_salary_text(text) == expected
