"""Parsers of the additional sources. Fixtures follow the formats documented/observed at
the time of writing; live endpoints were not reachable from the dev environment."""

from __future__ import annotations

import json

import pytest

from app.core.config import SourceConfig
from app.sources import habr, remoteok, remotive, superjob, trudvsem
from app.sources.base import SearchQuery, SourceError
from app.sources.jsonld import draft_from_html
from app.sources.registry import SOURCE_CLASSES
from app.sources.telegram_channels import TelegramChannelsSource, is_vacancy_post, parse_channel_page


def test_trudvsem_item():
    d = trudvsem.item_to_draft({
        "id": "abc-1", "job-name": "Программист Python", "company": {"name": "ГБУ Тест"},
        "region": {"name": "г. Москва"}, "salary_min": 90000, "salary_max": 0,
        "duty": "<p>Разработка</p>", "requirement": {"experience": 3, "qualification": "Python"},
        "schedule": "Удалённая работа", "vac_url": "https://trudvsem.ru/vacancy/card/1/abc-1",
        "creation-date": "2026-09-10",
    })
    assert d.salary_from == 90000 and d.salary_to is None and d.remote and d.experience == "от 3 лет"
    assert "Требования: Python" in d.description and d.published_at.month == 9
    assert trudvsem.item_to_draft({"id": "x"}) is None


def test_habr_item_and_page():
    d = habr.item_to_draft({
        "id": 1000123, "href": "/vacancies/1000123", "title": "Backend developer",
        "company": {"title": "Acme"}, "salary": {"from": 200000, "to": None, "currency": "rur"},
        "skills": [{"title": "Python"}, {"title": "Go"}], "locations": [{"title": "Москва"}],
        "remoteWork": True, "salaryQualification": {"title": "Middle"},
        "publishedDate": {"date": "2026-09-01T10:00:00+03:00"},
    })
    assert d.url == "https://career.habr.com/vacancies/1000123" and d.currency == "RUR"
    assert d.skills == ["Python", "Go"] and d.remote and d.is_partial
    page = '<script type="application/ld+json">{}</script>'.format(json.dumps(
        {"@type": "JobPosting", "title": "Backend developer", "description": "<p>Полное описание</p>"}))
    full = habr.parse_vacancy_page(page, "1000123")
    assert full.description == "Полное описание" and not full.is_partial
    html_only = '<h1>Dev</h1><div class="vacancy-description__text"><p>Текст</p></div>'
    assert habr.parse_vacancy_page(html_only, "1").description == "Текст"
    assert habr.HabrSource(SourceConfig()).external_id_from_url("https://career.habr.com/vacancies/42") == "42"


def test_remotive_and_remoteok_items():
    d = remotive.item_to_draft({"id": 7, "title": "Python Engineer", "company_name": "R",
                                "url": "https://remotive.com/x", "salary": "$50k - $70k",
                                "candidate_required_location": "Europe", "tags": ["python"],
                                "publication_date": "2026-09-01T00:00:00", "description": "<b>Hi</b>"})
    assert (d.salary_from, d.salary_to, d.currency) == (50000, 70000, "USD") and d.remote
    assert d.description.startswith("Требования к локации кандидата: Europe")
    assert remotive.parse_usd_range("up to $4,000/month") == (4000, None)
    o = remoteok.item_to_draft({"id": "9", "position": "Go Dev", "company": "O", "salary_min": 0,
                                "salary_max": 0, "tags": ["go"], "date": "2026-09-02T00:00:00+00:00"})
    assert o.salary_from is None and o.currency is None and o.remote


async def test_remoteok_filters_locally(monkeypatch):
    async def fake_fetch(*_a, **_k):
        return [{"legal": "notice"},
                {"id": "1", "position": "Python Dev", "tags": ["python"], "date": "2026-09-02"},
                {"id": "2", "position": "Designer", "tags": ["figma"], "date": "2026-09-03"}]

    monkeypatch.setattr(remoteok, "fetch_json", fake_fetch)
    drafts = await remoteok.RemoteOKSource(SourceConfig()).search(SearchQuery(text="python"), 10)
    assert [d.external_id for d in drafts] == ["1"]


async def test_superjob_requires_key(monkeypatch):
    monkeypatch.delenv("SUPERJOB_API_KEY", raising=False)
    with pytest.raises(SourceError, match="API-ключ"):
        await superjob.SuperJobSource(SourceConfig()).search(SearchQuery(text="python"), 10)
    d = superjob.item_to_draft({"id": 5, "profession": "Аналитик", "firm_name": "F", "payment_from": 100000,
                                "payment_to": 0, "currency": "rub", "town": {"title": "Москва"},
                                "place_of_work": {"title": "Удалённая работа"}, "date_published": 1790000000})
    assert d.currency == "RUR" and d.salary_to is None and d.remote and d.published_at.year >= 2026


TG_PAGE = """
<div class="tgme_widget_message" data-post="it_jobs/101">
  <div class="tgme_widget_message_text">🔥 #вакансия Python-разработчик<br>Компания: Acme<br>
  Требования: Python, FastAPI, 3+ года<br>Обязанности: развивать API<br>Зарплата: 250 000 – 300 000 ₽<br>
  Формат: удалённо. Откликнуться: @hr</div>
  <a class="tgme_widget_message_date"><time datetime="2026-09-20T10:00:00+00:00"></time></a>
</div>
<div class="tgme_widget_message" data-post="it_jobs/100">
  <div class="tgme_widget_message_text">#резюме Ищу работу Python-разработчиком, 3 года опыта, требования к зарплате 200к, удалённо, офис не рассматриваю</div>
</div>
<div class="tgme_widget_message" data-post="it_jobs/99">
  <div class="tgme_widget_message_text">Коротко: всем привет</div>
</div>
"""


def test_telegram_channel_page_classifies_posts():
    drafts, min_id = parse_channel_page(TG_PAGE, "it_jobs")
    assert min_id == "99" and len(drafts) == 1
    d = drafts[0]
    assert d.external_id == "it_jobs/101" and d.url == "https://t.me/it_jobs/101"
    assert d.title == "Python-разработчик"
    assert d.company == "Acme" and (d.salary_from, d.salary_to, d.currency) == (250000, 300000, "RUR")
    assert d.remote and d.published_at.day == 20
    assert not is_vacancy_post("Подписывайтесь на канал! Вакансии, требования, зарплата, удалённо " * 3)


async def test_telegram_source_without_channels_returns_nothing():
    assert await TelegramChannelsSource(SourceConfig()).search(SearchQuery(text="x"), 10) == []


def test_generic_jsonld_import():
    html = '<script type="application/ld+json">{}</script>'.format(json.dumps([{
        "@type": ["JobPosting"], "title": "QA", "hiringOrganization": "Beta",
        "baseSalary": {"currency": "RUB", "value": {"value": 150000}},
        "jobLocation": [{"address": "Казань"}], "skills": "SQL, Selenium",
        "description": "Тестирование",
    }]))
    d = draft_from_html(html, source="manual", external_id="x", url="https://ex.com/1")
    assert (d.company, d.location, d.salary_from, d.currency) == ("Beta", "Казань", 150000, "RUR")
    assert d.skills == ["SQL", "Selenium"]


def test_every_registered_source_has_unique_name_and_title():
    assert len({c.title for c in SOURCE_CLASSES.values()}) == len(SOURCE_CLASSES)
    for name, cls in SOURCE_CLASSES.items():
        assert cls.name == name and cls.title
