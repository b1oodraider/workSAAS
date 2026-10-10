"""Application settings.

Structure lives in ``config.toml`` (see ``config.example.toml``), secrets in
``.env`` / environment. Any value can be overridden with ``WS_`` prefixed env
vars, nested levels separated by ``__`` (e.g. ``WS_MATCHING__TOP_N=10``).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

CONFIG_PATH = Path(os.environ.get("WS_CONFIG", "config.toml"))


class ProviderConfig(BaseModel):
    """One LLM backend. ``type`` selects the adapter in ``app.llm.providers``."""

    type: Literal["anthropic", "openai_compat", "fake"]
    # Name of the env var holding the API key; keeps secrets out of config.toml.
    api_key_env: str = ""
    base_url: str | None = None
    use_proxy: bool = False
    timeout_s: float = 300.0
    max_retries: int = 2
    # anthropic only: server-side refusal fallback (beta "fallbacks": "default").
    refusal_fallback: bool = True
    # openai_compat only: how to ask for JSON. "json_schema" is best when supported,
    # "json_object" works with most servers (Ollama, LM Studio, DeepSeek).
    json_mode: Literal["json_schema", "json_object", "none"] = "json_object"
    # How the key is sent. anthropic: "x-api-key" (Anthropic) or "bearer" (most resellers).
    # openai_compat: "bearer" (default) or e.g. "Api-Key" for Yandex AI Studio API keys.
    auth_scheme: str = ""
    # Extra HTTP headers (e.g. a Yandex folder id), values may reference env vars as ${NAME}.
    headers: dict[str, str] = Field(default_factory=dict)
    # Reseller markup relative to the official price table (budget accounting), e.g. 2.4.
    price_multiplier: float = 1.0
    # openai_compat only: send the route's `effort` as "reasoning_effort" (gpt-oss, o-series:
    # limits paid "thinking" tokens). Off by default: some servers reject unknown parameters.
    send_reasoning_effort: bool = False

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    def resolved_headers(self) -> dict[str, str]:
        return {k: os.path.expandvars(v) for k, v in self.headers.items()}


class LLMTarget(BaseModel):
    provider: str
    model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    max_tokens: int | None = None


class LLMRoute(LLMTarget):
    """Which provider/model handles a given LLM task (by task name).

    ``fallbacks`` are tried in order when the primary is unavailable (network,
    rate limit, 5xx, auth/config error) — e.g. a reseller is down.
    """

    fallbacks: list[LLMTarget] = Field(default_factory=list)

    def targets(self) -> list[LLMTarget]:
        return [LLMTarget(provider=self.provider, model=self.model, effort=self.effort,
                          max_tokens=self.max_tokens), *self.fallbacks]


class ModelPrice(BaseModel):
    """USD per 1M tokens."""

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None


class EmbeddingSettings(BaseModel):
    """Meaning-based prefilter: resume and vacancy texts -> vectors (cached by text for all users).

    Off by default: needs an openai_compat provider with an /embeddings endpoint."""

    enabled: bool = False
    provider: str = ""
    model: str = ""
    # Share of the meaning similarity in the ranking score; the rest is the keyword score.
    weight: float = Field(0.5, ge=0.0, le=1.0)
    # Cosine similarity -> 0..100: at or below `low` gives 0, at or above `high` gives 100.
    # Depends on the model; 0.35..0.75 is a starting guess for bge-m3 (not measured).
    low: float = 0.35
    high: float = 0.75
    # Text cut before embedding (bge-m3 takes ~8k tokens; vacancies are rarely longer than this).
    max_chars: int = Field(6000, ge=500, le=20000)
    batch_size: int = Field(32, ge=1, le=256)
    # At most this many vacancies of one search run get vectors (cost cap for huge runs).
    max_per_run: int = Field(2000, ge=1)
    # Vectors are deleted this many days after they were stored, used or not (a bge-m3 vector is
    # ~2 KB; a text still being found is embedded again). Resume vectors are kept as long too.
    keep_days: int = Field(60, ge=1)


class LLMSettings(BaseModel):
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {
            "anthropic": ProviderConfig(type="anthropic", api_key_env="ANTHROPIC_API_KEY"),
            "fake": ProviderConfig(type="fake"),
        }
    )
    routes: dict[str, LLMRoute] = Field(
        default_factory=lambda: {
            "default": LLMRoute(provider="anthropic", model="claude-opus-5-5", effort="medium"),
            # Bulk tasks: same model, lower effort -> fewer thinking tokens.
            "match": LLMRoute(provider="anthropic", model="claude-opus-5-5", effort="low"),
            # The most frequent call (each found vacancy): route it to the cheapest model.
            "match_quick": LLMRoute(provider="anthropic", model="claude-opus-5-5", effort="low"),
            "resume_profile": LLMRoute(provider="anthropic", model="claude-opus-5-5", effort="low"),
            "vacancy_review": LLMRoute(provider="anthropic", model="claude-opus-5-5", effort="low"),
            "follow_up": LLMRoute(provider="anthropic", model="claude-opus-5-5", effort="low"),
            "recruiter_reply": LLMRoute(provider="anthropic", model="claude-opus-5-5", effort="low"),
        }
    )
    # Overrides / additions to app.llm.pricing.DEFAULT_PRICES.
    prices: dict[str, ModelPrice] = Field(default_factory=dict)
    cache_enabled: bool = True
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)

    def route_for(self, task: str) -> LLMRoute:
        return self.routes.get(task) or self.routes["default"]


class SourceConfig(BaseModel):
    enabled: bool = True
    use_proxy: bool = False
    # Source-specific options, validated by the source itself.
    options: dict[str, Any] = Field(default_factory=dict)


class MatchingSettings(BaseModel):
    # How many vacancies per search run go to the (paid) LLM match step.
    top_n: int = 15
    # Vacancies below this prefilter score (0..100) are never sent to LLM
    # (10 let through vacancies sharing a single secondary skill).
    prefilter_min: float = 20.0
    # Max vacancies fetched per query per source.
    fetch_limit: int = 50
    # Two-step LLM: a cheap `match_quick` (score + one line) for each of the top-N, the full
    # `match` only for those scoring at least `full_match_min`. false = full match for all top-N.
    quick_match: bool = True
    full_match_min: float = 60.0


class JobsSettings(BaseModel):
    concurrency: int = 2
    poll_interval_s: float = 1.0
    max_attempts: int = 2
    run_in_web_process: bool = True
    scheduler_interval_s: float = 60.0


class TelegramSettings(BaseModel):
    # Token from @BotFather; put it in .env as WS_TELEGRAM__BOT_TOKEN.
    bot_token: str | None = None
    enabled: bool = True
    use_proxy: bool = False
    # Default threshold for "good match" notifications (users can change theirs).
    notify_min_score: int = 75
    # How often to check for new good matches / reminders to send.
    digest_interval_s: float = 300.0
    # Base URL of the web UI for links in messages, e.g. https://jobs.example.com
    public_url: str = "http://127.0.0.1:8000"

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.bot_token)

    @property
    def is_https(self) -> bool:
        return self.public_url.strip().lower().startswith("https://")


class HHApplySelectors(BaseModel):
    """CSS selectors of hh.ru pages used by the browser applier.

    Written from hh.ru's public markup (data-qa attributes) and NOT verified against the
    live site from the development environment. If hh.ru changes its pages, fix them in
    config.toml ([apply.hh_selectors]) without touching code. Each value may list several
    selectors separated by commas (CSS "or").
    """

    response_button: str = '[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"]'
    already_applied: str = '[data-qa="vacancy-response-link-view-topic"]'
    relocation_confirm: str = '[data-qa="relocation-warning-confirm"]'
    resume_option: str = '[data-qa="resume-title"]'
    letter_toggle: str = '[data-qa="vacancy-response-letter-toggle"]'
    letter_input: str = ('[data-qa="vacancy-response-popup-form-letter-input"], '
                         'textarea[name="letter"]')
    submit: str = '[data-qa="vacancy-response-submit-popup"]'
    success: str = ('[data-qa="vacancy-response-success-standard-notification"], '
                    '[data-qa="vacancy-response-link-view-topic"]')
    login_form: str = '[data-qa="account-login-form"], [data-qa="login"]'
    # A card in the applicant's "Отклики" list (status sync); empty = find cards by vacancy links.
    negotiations_item: str = '[data-qa*="negotiations-item"]'


class ApplySettings(BaseModel):
    """Auto-apply guard rails. Users choose their own limits within these caps."""

    enabled: bool = True
    hard_daily_max: int = 30          # no user can go above this per day
    min_interval_floor_s: int = 60    # and never faster than one application per this many seconds
    max_consecutive_failures: int = 3  # then auto-apply pauses itself
    # "auto" mode sends without asking, so the LLM score (which a vacancy text may sway) must be
    # backed by the keyword prefilter (0..100): above 30 = the title alone is not enough, resume
    # skills must be found in the text too.
    auto_min_prefilter: float = 40.0
    # Read the user's hh.ru "Отклики" (statuses, applications made by hand) every N hours; 0 = off.
    status_sync_hours: int = 6
    headless: bool = True
    # Optional path to a Chrome/Chromium binary instead of Playwright's bundled one.
    browser_executable: str = ""
    # Route the applier's browser through `proxy_url`, e.g. when the server is abroad and
    # hh.ru should see a Russian IP like the one the session was created from.
    use_proxy: bool = False
    hh_selectors: HHApplySelectors = Field(default_factory=HHApplySelectors)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WS_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = "sqlite:///data/worksaas.db"
    secret_key: str = "change-me"
    # Single outbound proxy; each provider/source opts in with use_proxy=true.
    proxy_url: str | None = None
    # Language for LLM outputs (cover letters, reviews).
    output_language: str = "русский"
    # Times are stored in UTC; the UI shows and accepts them in this time zone.
    timezone: str = "Europe/Moscow"
    default_monthly_budget_usd: float = 10.0
    # Secure session cookie. On automatically when telegram.public_url starts with https://;
    # set true to force it (e.g. HTTPS without a public_url).
    session_https_only: bool = False
    # Addresses of the reverse proxy (Caddy/nginx) allowed to set X-Forwarded-For/-Proto, comma
    # separated. Login throttling needs the real client IP. Default: proxy on the same host. In
    # docker the proxy is seen as the network gateway (e.g. "172.17.0.1"); "*" trusts everyone —
    # only when the app port is reachable from the proxy alone.
    trusted_proxies: str = "127.0.0.1"
    max_upload_mb: int = 10

    llm: LLMSettings = Field(default_factory=LLMSettings)
    sources: dict[str, SourceConfig] = Field(
        default_factory=lambda: {
            "hh": SourceConfig(options={"area": 113}),
            "manual": SourceConfig(),
            "rss": SourceConfig(enabled=False, options={"feeds": []}),
        }
    )
    matching: MatchingSettings = Field(default_factory=MatchingSettings)
    jobs: JobsSettings = Field(default_factory=JobsSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    apply: ApplySettings = Field(default_factory=ApplySettings)
    # Where per-user files live (browser sessions for auto-apply, backups).
    data_dir: str = "data"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=CONFIG_PATH),
        )


@lru_cache
def get_settings() -> Settings:
    # Provider API keys are read via os.environ (api_key_env), so load .env there too.
    load_dotenv(override=False)
    return Settings()
