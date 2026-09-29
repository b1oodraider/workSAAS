from __future__ import annotations

import os

os.environ["WS_CONFIG"] = "/nonexistent/config.toml"  # ignore developer's local config
os.environ.setdefault("WS_SECRET_KEY", "test-secret")

import pytest  # noqa: E402

from app.bot.api import set_api  # noqa: E402
from app.core.config import LLMRoute, get_settings  # noqa: E402
from app.core.db import create_all, init_engine, session_scope  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.llm.gateway import LLMGateway, set_gateway  # noqa: E402
from app.llm.providers.fake import FakeProvider  # noqa: E402
from app.models import User  # noqa: E402
from app.plugins import load_all  # noqa: E402
from app.sources.web import response_cache  # noqa: E402

load_all()

RESUME_TEXT = """Иван Петров — Python backend-разработчик, 4 года опыта.
Опыт: ООО Ромашка (2021–2025) — разработка API на FastAPI и PostgreSQL, Docker, Redis,
ускорил выдачу каталога в 3 раза. Навыки: Python, FastAPI, Django, PostgreSQL, Redis, Docker, Kafka.
"""

VACANCY_TEXT = """Ищем Python-разработчика в команду платежей. Задачи: развитие API на FastAPI,
работа с PostgreSQL и Kafka. Требования: Python 3+, опыт от 3 лет, Docker. Удалённо."""


@pytest.fixture(autouse=True)
def env(tmp_path):
    settings = get_settings()
    snapshot = settings.model_copy(deep=True)  # tests mutate settings; restore afterwards
    settings.llm.routes = {"default": LLMRoute(provider="fake", model="fake-model")}
    settings.default_monthly_budget_usd = 0
    settings.telegram.bot_token = None  # never talk to the real Telegram from tests
    set_api(None)
    response_cache.clear()
    init_engine(f"sqlite:///{tmp_path / 'test.db'}")
    create_all()
    FakeProvider.canned.clear()
    FakeProvider.calls.clear()
    set_gateway(LLMGateway(settings))
    yield settings
    set_gateway(None)
    for field in type(settings).model_fields:
        setattr(settings, field, getattr(snapshot, field))


@pytest.fixture
def user_id() -> int:
    with session_scope() as s:
        user = User(username="alice", password_hash=hash_password("password123"), is_admin=True)
        s.add(user)
        s.flush()
        return user.id
