"""Russian and opt-in HTML sources: Яндекс, Работа.ру, Geekjob, Remote-job.ru, Авито, LinkedIn.

Fixtures in tests/fixtures/sources/ are trimmed copies of real responses fetched live on
2026-10-08 (2-3 cards each, long texts cut). No network: fetchers and fetch_json are faked.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.core.config import SourceConfig
from app.sources import geekjob, linkedin, rabota_ru, remote_job_ru, yandex_jobs
from app.sources.avito_jobs import AvitoJobsSource
from app.sources.avito_jobs import parse_search_page as avito_parse
from app.sources.base import SearchFilters, SearchQuery, SourceError
from app.sources.web import Page

FIXTURES = Path(__file__).parent / "fixtures" / "sources"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFetcher:
    def __init__(self, pages: list[Page]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict | None]] = []
        self.closed = False

    async def get(self, url, params=None):
        self.calls.append((url, params))
        return self.pages.pop(0)

    async def close(self):
        self.closed = True


def fake_json(monkeypatch, module, responses):
    """Replace module.fetch_json; returns the list of (url, params) calls."""
    calls: list[tuple[str, dict | None]] = []
    queue = list(responses)

    async def _fetch_json(url, *, params=None, **_kw):
        calls.append((url, dict(params or {})))
        return queue.pop(0)

    monkeypatch.setattr(module, "fetch_json", _fetch_json)
    return calls


# --------------------------------------------------------------------------- Яндекс


def test_yandex_search_item():
    data = json.loads(fixture("yandex_jobs_search.json"))
    d = yandex_jobs.item_to_draft(data["results"][0])
    assert d.external_id == "15322" and d.company == "Яндекс" and d.is_partial
    assert d.url == "https://yandex.ru/jobs/vacancies/multitrack-_-noviy-format-nayma-dlya-opitnih-bekenderov-15322"
    assert d.location == "Москва, Санкт-Петербург" and d.skills == ["Python", "C++", "Java", "Go"]
    assert "Сервис: Общие сервисы Яндекса" in d.description and "Формат работы: Гибридный" in d.description
    assert d.remote is None and d.published_at.date().isoformat() == "2026-10-07"
    assert yandex_jobs.next_cursor(data) == "cD0wJnQ9MjAyNi0xMC0wNSZpPTQ5MTc5JnI9MCZrPTAuMjczNTY3Mg"
    assert yandex_jobs.item_to_draft({"id": 1}) is None


def test_yandex_card_has_full_text():
    d = yandex_jobs.card_to_draft(json.loads(fixture("yandex_jobs_card.json")))
    assert not d.is_partial and d.experience == "senior"
    assert "Ключевые требования:\n* Уверенно владеете Java" in d.description
    assert "Будет плюсом:" in d.description and d.published_at == datetime(2026, 2, 11)
    # A card without any text section stays partial and falls back to the summary.
    bare = yandex_jobs.card_to_draft({"id": 5, "title": "X", "short_summary": "Кратко"})
    assert bare.is_partial and bare.description == "Кратко"


async def test_yandex_search_paginates_by_cursor_and_filters_remote(monkeypatch):
    page1 = json.loads(fixture("yandex_jobs_search.json"))
    page2 = {"count": 3, "results": [{**page1["results"][0], "id": 99, "publication_slug_url": "x-99"}]}
    calls = fake_json(monkeypatch, yandex_jobs, [page1, page2])
    src = yandex_jobs.YandexJobsSource(SourceConfig())
    drafts = await src.search(SearchQuery(text="python", filters=SearchFilters(remote_only=True)), limit=10)
    assert [d.external_id for d in drafts] == ["15322", "37135", "99"]
    assert calls[0][1]["work_modes"] == "remote" and "cursor" not in calls[0][1]
    assert calls[1][1]["cursor"] == yandex_jobs.next_cursor(page1)  # no "next" in page2 -> stop
    assert len(calls) == 2


async def test_yandex_fetch_and_url(monkeypatch):
    calls = fake_json(monkeypatch, yandex_jobs, [json.loads(fixture("yandex_jobs_card.json"))])
    src = yandex_jobs.YandexJobsSource(SourceConfig())
    d = await src.fetch("15322")
    assert calls[0][0] == "https://yandex.ru/jobs/api/publications/15322" and not d.is_partial
    assert await src.fetch("../etc") is None
    assert src.external_id_from_url("https://yandex.ru/jobs/vacancies/razrabotchik-v-yandeks-id-37135?from=x") == "37135"
    assert src.external_id_from_url("https://yandex.ru/jobs/vacancies") is None


# --------------------------------------------------------------------------- Работа.ру


def test_rabota_search_page_json_ld_list():
    drafts = rabota_ru.parse_search_page(fixture("rabota_ru_search.html"))
    assert [d.external_id for d in drafts] == ["54436186", "54428627", "54422703"]
    d = drafts[0]
    assert d.title == "Senior Python-разработчик в RnD-лабораторию" and d.location == "Москва"
    # minValue sits on the MonetaryAmount itself, maxValue 0 means "not set".
    assert (d.salary_from, d.salary_to, d.currency) == (254100, None, "RUR")
    assert not d.is_partial and d.description.startswith("Мы ищем Senior Python")
    assert drafts[1].salary_from is None and drafts[1].currency is None
    assert d.url == "https://www.rabota.ru/vacancy/54436186/"


def test_rabota_vacancy_page_microdata_and_nuxt():
    d = rabota_ru.parse_vacancy_page(fixture("rabota_ru_card.html"), "54436186")
    assert d.company == 'АНО ДПО "Корпоративный Университет Сбербанка"' and d.location == "Москва"
    assert d.salary_from == 254100 and d.currency == "RUR" and d.employment == "полный рабочий день"
    assert d.description.startswith("Мы ищем") and "Нам нужен сильный разработчик" in d.description
    assert not d.is_partial and d.published_at == datetime(2026, 10, 6, 9, 54, 11)
    # Another vacancy id: the NUXT description must not be borrowed from a different object.
    assert rabota_ru.parse_vacancy_page(fixture("rabota_ru_card.html"), "1").is_partial
    assert rabota_ru.parse_vacancy_page("<html></html>", "1") is None


async def test_rabota_search_stops_on_short_page_and_reports_block(monkeypatch):
    src = rabota_ru.RabotaRuSource(SourceConfig())
    fetcher = FakeFetcher([Page("https://www.rabota.ru/vacancy/", 200, fixture("rabota_ru_search.html"))])
    monkeypatch.setattr(src, "_fetcher", lambda: fetcher)
    drafts = await src.search(SearchQuery(text="python"), limit=50)
    assert len(drafts) == 3 and fetcher.calls == [("https://www.rabota.ru/vacancy/", {"query": "python"})]
    assert fetcher.closed

    blocked = FakeFetcher([Page("https://www.rabota.ru/vacancy/", 403, "captcha")])
    monkeypatch.setattr(src, "_fetcher", lambda: blocked)
    with pytest.raises(SourceError, match="Работа.ру"):
        await src.search(SearchQuery(text="python"), limit=5)
    assert src.external_id_from_url("https://spb.rabota.ru/vacancy/54436186/?search_id=1") == "54436186"


# --------------------------------------------------------------------------- Geekjob


def test_geekjob_items():
    items = json.loads(fixture("geekjob_search.json"))["data"]
    drafts = [geekjob.item_to_draft(i) for i in items]
    assert [d.external_id for d in drafts] == ["6ab4ca3a5a37344b7508650d", "6ac37fb023481b12a406c4a5",
                                               "6ac614a70e7ce911650091be"]
    d = drafts[1]
    assert d.company == "Mad Devs" and d.location == "Бишкек, Кыргызстан" and d.remote and d.is_partial
    assert (d.salary_from, d.salary_to) == (2300, 2800)  # "2.3K — 2.8K ₽"
    assert d.published_at == datetime(2026, 10, 5)
    assert drafts[2].remote is None and "в офисе" in drafts[2].description
    assert geekjob.item_to_draft({"id": "../x", "position": "Dev"}) is None


def test_geekjob_vacancy_page():
    d = geekjob.parse_vacancy_page(fixture("geekjob_card.html"), "6ac37fb023481b12a406c4a5")
    assert d.title == "Applied AI / LLM Engineer (Python)" and d.company == "Mad Devs"
    # The JSON-LD address says London; the page header (what users see) wins.
    assert d.location == "Бишкек, Кыргызстан"
    assert d.remote and d.experience == "Опыт работы от 3 до 5 лет" and "Python" in d.skills
    assert not d.is_partial and d.description.startswith("We're looking for an AI Engineer")


async def test_geekjob_search_and_fetch(monkeypatch):
    data = json.loads(fixture("geekjob_search.json"))
    calls = fake_json(monkeypatch, geekjob, [data])
    src = geekjob.GeekjobSource(SourceConfig())
    drafts = await src.search(SearchQuery(text="python", filters=SearchFilters(remote_only=True)), limit=10)
    assert len(drafts) == 3 and len(calls) == 1  # nextpage == 0
    assert calls[0] == ("https://geekjob.ru/json/find/vacancy", {"page": 1, "qs": "python", "rm": 1})

    fetcher = FakeFetcher([Page("https://geekjob.ru/vacancy/x", 200, fixture("geekjob_card.html"))])
    monkeypatch.setattr(src, "_fetcher", lambda: fetcher)
    full = await src.fetch("6ac37fb023481b12a406c4a5")
    assert full and not full.is_partial
    assert await src.fetch("not-an-id") is None
    assert src.external_id_from_url("https://geekjob.ru/vacancy/6ac37fb023481b12a406c4a5") == \
        "6ac37fb023481b12a406c4a5"


# --------------------------------------------------------------------------- Remote-job.ru


def test_remote_job_search_page():
    drafts = remote_job_ru.parse_search_page(fixture("remote_job_ru_search.html"))
    assert [d.external_id for d in drafts] == ["997095", "1521446", "1521402"]
    d = drafts[1]
    # The search highlight <span> must not leave stray spaces, the "(удаленная работа)" suffix is dropped.
    assert d.title == "Тестировщик-автоматизатор (Python)" and d.company == "Volna.tech"
    assert (d.salary_from, d.salary_to, d.currency) == (130000, 150000, "RUR")
    assert d.remote and d.is_partial and not d.description.startswith("Описание вакансии")


def test_remote_job_vacancy_page():
    d = remote_job_ru.parse_vacancy_page(fixture("remote_job_ru_card.html"), "997095")
    assert d.title == "Автотестировщик Java/Python (с опытом)" and d.company == "WorkTech"
    assert (d.salary_from, d.salary_to) == (55000, 200000) and d.experience == "Без опыта"
    assert d.published_at == datetime(2026, 6, 15) and not d.is_partial
    assert "Вакансия:" not in d.description and "Откликнуться" not in d.description
    assert d.description.endswith("присоединяйся к нам!")


@pytest.mark.parametrize("text,expected", [
    ("6 октября 2026", datetime(2026, 10, 6)),
    ("  15 июня 2026 ", datetime(2026, 6, 15)),
    ("31 февраля 2026", None),
    ("вчера", None),
])
def test_parse_ru_date(text, expected):
    assert remote_job_ru.parse_ru_date(text) == expected


async def test_remote_job_search_paging(monkeypatch):
    src = remote_job_ru.RemoteJobRuSource(SourceConfig(options={"pages": 3}))
    fetcher = FakeFetcher([Page("https://remote-job.ru/search", 200, fixture("remote_job_ru_search.html"))])
    monkeypatch.setattr(src, "_fetcher", lambda: fetcher)
    drafts = await src.search(SearchQuery(text="python"), limit=10)
    assert len(drafts) == 3 and len(fetcher.calls) == 1  # 3 < 20 cards: last page
    assert fetcher.calls[0] == ("https://remote-job.ru/search", {"search[query]": "python"})
    assert src.external_id_from_url("https://remote-job.ru/vacancy/show/1521402/python-razrabotchik") == "1521402"


# --------------------------------------------------------------------------- Авито


def test_avito_embedded_state():
    drafts = avito_parse(fixture("avito_jobs_search.html"))
    # The widget and the "Похоже на ваш поиск" item are skipped.
    assert [d.external_id for d in drafts] == ["7539615390", "4150590948"]
    d = drafts[0]
    assert d.url == "https://www.avito.ru/nizhnekamsk/vakansii/rabochiy_na_proizvodstvo_7539615390"
    assert (d.salary_from, d.salary_to, d.currency, d.salary_gross) == (75000, 130000, "RUR", False)
    assert d.company == "Питон Электрик" and d.is_partial and "\xa0" not in d.description
    assert d.published_at == datetime(2026, 10, 7, 9, 42, 1)
    similar = avito_parse(fixture("avito_jobs_search.html"), include_similar=True)
    assert len(similar) == 3 and similar[2].title == "Программист 1С"
    assert avito_parse("<html>no state</html>") == []


async def test_avito_is_opt_in_untrusted_and_reports_block(monkeypatch):
    assert not AvitoJobsSource.enabled_by_default and not AvitoJobsSource.trusted
    src = AvitoJobsSource(SourceConfig())
    blocked = FakeFetcher([Page("https://www.avito.ru/all/vakansii", 439,
                                "<title>Доступ ограничен: проверка безопасности</title>")])
    monkeypatch.setattr(src, "_fetcher", lambda: blocked)
    with pytest.raises(SourceError, match="Авито"):
        await src.search(SearchQuery(text="python"), limit=10)
    ok = FakeFetcher([Page("https://www.avito.ru/all/vakansii", 200, fixture("avito_jobs_search.html"))])
    monkeypatch.setattr(src, "_fetcher", lambda: ok)
    assert len(await src.search(SearchQuery(text="python"), limit=10)) == 2
    assert ok.calls == [("https://www.avito.ru/all/vakansii", {"q": "python"})]
    with pytest.raises(SourceError):
        await AvitoJobsSource(SourceConfig(options={"location": "../x"})).search(SearchQuery(text="a"), 1)
    assert await src.fetch("7539615390") is None  # cards are not read (anti-bot)


# --------------------------------------------------------------------------- LinkedIn


def test_linkedin_search_cards():
    drafts = linkedin.parse_search_page(fixture("linkedin_search.html"))
    assert [d.external_id for d in drafts] == ["4192503234", "4471319619"]
    d = drafts[0]
    assert d.url == "https://www.linkedin.com/jobs/view/4192503234/" and d.company == "BairesDev"
    assert d.remote and d.is_partial and d.published_at == datetime(2025, 3, 28)
    assert drafts[1].remote is None


def test_linkedin_job_page():
    d = linkedin.parse_job_page(fixture("linkedin_card.html"), "4192503234")
    assert d.title == "Python Junior Developer - Remote Work | REF#283516" and d.company == "BairesDev"
    assert d.experience == "Стажер" and d.employment == "Полный рабочий день"
    assert d.description.startswith("At BairesDev") and not d.is_partial
    assert linkedin.parse_job_page("<div></div>", "1") is None


async def test_linkedin_one_request_per_location(monkeypatch):
    assert not linkedin.LinkedInSource.enabled_by_default
    locations = ["Germany", "Netherlands", "Remote", "Poland", "Spain", "Italy", "France"]
    src = linkedin.LinkedInSource(SourceConfig(options={"locations": locations, "min_interval": 1}))
    page = Page(linkedin.SEARCH_URL, 200, fixture("linkedin_search.html"))
    fetcher = FakeFetcher([page] * 5)
    monkeypatch.setattr(src, "_fetcher", lambda: fetcher)
    query = SearchQuery(text="python", filters=SearchFilters(remote_only=True, period_days=7))
    drafts = await src.search(query, limit=50)
    assert len(fetcher.calls) == linkedin.MAX_LOCATIONS  # capped, one page each
    _url, params = fetcher.calls[0]
    assert params == {"keywords": "python", "location": "Germany", "start": 0, "sortBy": "DD",
                      "f_TPR": "r604800", "f_WT": 2}
    assert len(drafts) == 2 and all(d.remote for d in drafts)


async def test_linkedin_rate_limit_and_urls(monkeypatch):
    src = linkedin.LinkedInSource(SourceConfig())
    fetcher = FakeFetcher([Page(linkedin.SEARCH_URL, 429, "")])
    monkeypatch.setattr(src, "_fetcher", lambda: fetcher)
    with pytest.raises(SourceError, match="429"):
        await src.search(SearchQuery(text="python"), limit=10)
    assert fetcher.calls[0][1]["location"] == "Worldwide"
    view = "https://br.linkedin.com/jobs/view/python-junior-developer-remote-work-ref%23283516-at-bairesdev-4192503234?position=1"
    assert src.external_id_from_url(view) == "4192503234"
    assert src.external_id_from_url("https://www.linkedin.com/jobs/search/?currentJobId=4471319619&keywords=x") == \
        "4471319619"
    assert src.external_id_from_url("https://www.linkedin.com/posts/someone_hiring-activity-123456789") is None
