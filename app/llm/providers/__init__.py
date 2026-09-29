"""Provider registry: config ``type`` -> adapter class."""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import ProviderConfig
from app.llm.base import LLMError, LLMProvider


def _anthropic(name: str, cfg: ProviderConfig) -> LLMProvider:
    from app.llm.providers.anthropic import AnthropicProvider

    return AnthropicProvider(name, cfg)


def _openai_compat(name: str, cfg: ProviderConfig) -> LLMProvider:
    from app.llm.providers.openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(name, cfg)


def _fake(name: str, cfg: ProviderConfig) -> LLMProvider:
    from app.llm.providers.fake import FakeProvider

    return FakeProvider(name)


FACTORIES: dict[str, Callable[[str, ProviderConfig], LLMProvider]] = {
    "anthropic": _anthropic,
    "openai_compat": _openai_compat,
    "fake": _fake,
}


def build_provider(name: str, cfg: ProviderConfig) -> LLMProvider:
    try:
        factory = FACTORIES[cfg.type]
    except KeyError:
        raise LLMError(f"Unknown LLM provider type: {cfg.type}") from None
    return factory(name, cfg)
