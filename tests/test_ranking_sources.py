from __future__ import annotations

import io

from app.documents.extract import extract_text
from app.ranking.prefilter import KeywordRanker, RankInput, RankProfile
from app.sources.base import SearchFilters
from app.sources.hh import HHSource, item_to_draft
from app.sources.rss import parse_feed
from app.core.config import SourceConfig

PROFILE = RankProfile(core_skills=["Python", "FastAPI", "PostgreSQL"], secondary_skills=["Kafka"],
                      roles=["Python-разработчик", "Backend developer"], negative_keywords=["1С"])


def test_ranker_scores_relevant_higher():
    r = KeywordRanker()
    good = r.score(PROFILE, RankInput("Backend developer (Python)", "FastAPI, PostgreSQL, Kafka"), SearchFilters())
    bad = r.score(PROFILE, RankInput("Менеджер по продажам", "Холодные звонки"), SearchFilters())
    assert good.score > 80 and bad.score == 0
    assert any("FastAPI" in x for x in good.reasons)


def test_ranker_hard_filters():
    r = KeywordRanker()
    f = SearchFilters(remote_only=True, salary_min=200_000, exclude_words=["битрикс"])
    assert r.score(PROFILE, RankInput("Python dev", "Python", remote=False), f).excluded
    assert r.score(PROFILE, RankInput("Python dev", "Python", salary_to=100_000), f).excluded
    assert r.score(PROFILE, RankInput("Python dev", "Python и Битрикс"), f).excluded
    assert r.score(PROFILE, RankInput("Программист 1С", "1С"), SearchFilters()).excluded
    assert not r.score(PROFILE, RankInput("Python dev", "Python", remote=True, salary_to=300_000), f).excluded


def test_hh_item_to_draft_full_and_snippet():
    item = {
        "id": "123", "name": "Python developer", "alternate_url": "https://hh.ru/vacancy/123",
        "employer": {"name": "Acme"}, "area": {"name": "Москва"},
        "salary": {"from": 200000, "to": None, "currency": "RUR", "gross": False},
        "schedule": {"id": "remote"}, "experience": {"name": "1–3 года"},
        "snippet": {"requirement": "Знание <highlighttext>Python</highlighttext>", "responsibility": "Писать код"},
        "description": "<p>Мы ищем</p><ul><li>Python</li><li>SQL</li></ul>",
        "key_skills": [{"name": "Python"}, {"name": "SQL"}],
        "published_at": "2026-09-01T10:00:00+0300",
    }
    snippet = item_to_draft(item, full=False)
    assert snippet.is_partial and "Знание Python" in snippet.description and snippet.remote
    full = item_to_draft(item, full=True)
    assert not full.is_partial and "• Python" in full.description and full.skills == ["Python", "SQL"]
    assert full.salary_from == 200000 and full.published_at.year == 2026
    assert HHSource(SourceConfig()).external_id_from_url("https://hh.ru/vacancy/98765?from=x") == "98765"


def test_rss_and_atom_parsing():
    rss = """<rss><channel><item><title>Python dev</title><link>https://ex.com/1</link>
      <guid>g1</guid><description>&lt;b&gt;FastAPI&lt;/b&gt;</description>
      <pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    items = parse_feed(rss, "x")
    assert items[0].title == "Python dev" and items[0].description == "FastAPI"
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Go dev</title>
      <link href="https://ex.com/2"/><id>e2</id><summary>Go, gRPC</summary>
      <updated>2026-09-01T10:00:00Z</updated></entry></feed>"""
    assert parse_feed(atom, "y")[0].url == "https://ex.com/2"


def test_docx_extraction():
    from docx import Document

    doc = Document()
    doc.add_paragraph("Иван Петров, Python-разработчик с опытом работы более четырёх лет в финтехе")
    buf = io.BytesIO()
    doc.save(buf)
    assert "Python-разработчик" in extract_text("cv.docx", buf.getvalue())


def test_salary_filter_ignores_foreign_currency_and_stop_words_match_word_forms():
    r = KeywordRanker()
    f = SearchFilters(salary_min=200_000, exclude_words=["гемблинг"])
    assert not r.score(PROFILE, RankInput("Python dev", "Python", salary_to=6000, currency="USD"), f).excluded
    assert r.score(PROFILE, RankInput("Python dev", "Python", salary_to=100_000, currency="RUR"), f).excluded
    assert r.score(PROFILE, RankInput("Python dev", "Разработка для гемблинга"), f).excluded
    # skills stay whole-token: Java must not match JavaScript
    java = RankProfile(core_skills=["Java"])
    assert r.score(java, RankInput("Frontend", "JavaScript, React"), SearchFilters()).score == 0


def test_openai_compat_schema_is_inlined():
    from app.features.match.schema import MatchResult
    from app.llm.base import LLMRequest

    schema = LLMRequest(task="t", model="m", system="", user="", output_type=MatchResult).inline_json_schema
    assert "$defs" not in schema and "$ref" not in str(schema)
    assert schema["properties"]["gaps"]["items"]["properties"]["importance"]["enum"] == ["must", "nice"]


def test_inline_schema_keeps_fields_named_title():
    from pydantic import BaseModel

    from app.llm.base import LLMRequest

    class WithTitle(BaseModel):
        title: str

    schema = LLMRequest(task="t", model="m", system="", user="", output_type=WithTitle).inline_json_schema
    assert "title" in schema["properties"] and "title" not in schema


def test_stop_words_do_not_overmatch_latin_prefixes():
    r = KeywordRanker()
    f = SearchFilters(exclude_words=["Java", "go"])
    assert not r.score(PROFILE, RankInput("Senior JavaScript", "React"), f).excluded
    assert not r.score(PROFILE, RankInput("Python dev", "Работа в Google"), f).excluded
    assert r.score(PROFILE, RankInput("Java dev", "Spring"), f).excluded


def test_malformed_items_are_skipped_not_fatal():
    from app.sources import habr, trudvsem
    from app.sources.base import safe_map
    from app.sources.jsonld import draft_from_job_posting

    items = [{"id": 1, "title": "Ok"}, {"id": 2, "title": "Bad", "salary": {"from": "много"}}, "junk"]
    drafts = safe_map(habr.item_to_draft, items, source="habr")
    assert [d.external_id for d in drafts] == ["1"]
    jp = {"title": "QA", "jobLocation": {"address": {"addressCountry": {"@type": "Country", "name": "RU"}}},
          "hiringOrganization": {"name": {"x": 1}}, "baseSalary": {"value": "1e999"}}
    d = draft_from_job_posting(jp, source="manual", external_id="1", url=None)
    assert d.location == "RU" and d.company is None and d.salary_from is None
    assert safe_map(lambda w: trudvsem.item_to_draft(w["vacancy"]), [{"no": "vacancy"}]) == []
