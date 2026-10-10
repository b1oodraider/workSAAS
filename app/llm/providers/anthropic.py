"""Claude via the official Anthropic SDK (structured outputs with messages.parse)."""

from __future__ import annotations

import logging
from typing import Any

import anthropic
from pydantic import ValidationError

from app.core.config import ProviderConfig, get_settings
from app.llm.base import (
    LLMError,
    LLMInvalidOutput,
    LLMRefusal,
    LLMRequest,
    LLMResponse,
    LLMUnavailable,
    TokenUsage,
    parse_json_output,
)

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

log = logging.getLogger(__name__)


def _with_detail(err: LLMError, exc: Exception) -> LLMError:
    """Keep the SDK's message (it may quote the response body) out of str(err), in logs instead."""
    err.detail = str(getattr(exc, "message", "") or exc)[:500]
    log.warning("%s: %s", err, err.detail)
    return err


class AnthropicProvider:
    def __init__(self, name: str, cfg: ProviderConfig) -> None:
        self.name = name
        self.cfg = cfg
        kwargs: dict[str, Any] = {"timeout": cfg.timeout_s, "max_retries": cfg.max_retries}
        if cfg.api_key:
            # Resellers usually expect "Authorization: Bearer <key>" instead of x-api-key.
            if cfg.auth_scheme.lower() == "bearer":
                kwargs["auth_token"] = cfg.api_key
            else:
                kwargs["api_key"] = cfg.api_key
        if cfg.headers:
            kwargs["default_headers"] = cfg.resolved_headers()
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        proxy = get_settings().proxy_url
        if cfg.use_proxy and proxy:
            kwargs["http_client"] = anthropic.DefaultAsyncHttpxClient(proxy=proxy)
        self.client = anthropic.AsyncAnthropic(**kwargs)

    def _use_fallback(self, model: str) -> bool:
        # Server-side fallbacks exist on the Claude API itself, not on custom gateways.
        return self.cfg.refusal_fallback and not self.cfg.base_url and model in _FALLBACK_MODELS

    async def generate(self, req: LLMRequest) -> LLMResponse:
        system_block: dict[str, Any] = {"type": "text", "text": req.system}
        if req.cache_system:
            system_block["cache_control"] = {"type": "ephemeral"}

        kwargs: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "system": [system_block],
            "messages": [{"role": "user", "content": req.user}],
            "output_format": req.output_type,
        }
        if req.effort:
            kwargs["output_config"] = {"effort": req.effort}
        if self._use_fallback(req.model):
            kwargs["extra_headers"] = {"anthropic-beta": _FALLBACK_BETA}
            kwargs["extra_body"] = {"fallbacks": "default"}

        try:
            response = await self.client.messages.parse(**kwargs)
        except ValidationError as exc:  # the SDK validates truncated/refused text itself
            raise LLMInvalidOutput(f"Output does not match schema: {exc}") from exc
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
            raise _with_detail(LLMUnavailable(f"{self.name}: temporarily unavailable ({type(exc).__name__})"),
                               exc) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise _with_detail(LLMUnavailable(f"{self.name}: server error {exc.status_code}"), exc) from exc
            # The SDK message carries the response body: logs only, the user sees the status code.
            raise _with_detail(LLMError(f"{self.name}: API error {exc.status_code}"), exc) from exc

        u = response.usage
        usage = TokenUsage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
        )
        try:
            if response.stop_reason == "refusal":
                details = response.stop_details
                reason = getattr(details, "explanation", None) or getattr(details, "category", None)
                raise LLMRefusal(f"Model declined the request ({reason or 'no details'})")
            if response.stop_reason == "max_tokens":
                raise LLMInvalidOutput("Response truncated by max_tokens")
            parsed = getattr(response, "parsed_output", None)
            if parsed is not None:
                data = parsed.model_dump(mode="json")
            else:
                text = "".join(b.text for b in response.content if b.type == "text")
                data = parse_json_output(text, req.output_type)
        except LLMError as exc:
            exc.usage = usage
            raise
        return LLMResponse(data=data, model=response.model or req.model, usage=usage)
