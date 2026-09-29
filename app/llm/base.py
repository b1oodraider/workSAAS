"""Provider-neutral LLM contracts. Providers turn an LLMRequest into JSON data."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError


class LLMError(Exception):
    """Non-retryable LLM failure (bad config, bad request, auth)."""

    retryable = False


class LLMUnavailable(LLMError):
    """Temporary failure: rate limit, network, 5xx. The job may be retried."""

    retryable = True


class LLMRefusal(LLMError):
    """The model declined to answer (stop_reason == "refusal")."""


class LLMInvalidOutput(LLMError):
    """Output was truncated or did not match the schema."""

    retryable = True


class BudgetExceeded(LLMError):
    """User's monthly LLM budget is spent."""


@dataclass
class LLMRequest:
    task: str
    model: str
    system: str
    user: str
    output_type: type[BaseModel]
    max_tokens: int = 16000
    effort: str | None = None
    # Hint: system prompt is reused across many calls (e.g. resume in bulk matching).
    cache_system: bool = False

    @property
    def json_schema(self) -> dict[str, Any]:
        return self.output_type.model_json_schema()


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class LLMResponse:
    data: dict[str, Any]
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)


class LLMProvider(Protocol):
    name: str

    async def generate(self, req: LLMRequest) -> LLMResponse: ...


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)


def parse_json_output(text: str, output_type: type[BaseModel]) -> dict[str, Any]:
    """Parse model text into a dict validated by ``output_type``.

    Tolerates markdown code fences and leading/trailing prose around a JSON object,
    which weaker/local models often add.
    """
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        raw = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LLMInvalidOutput(f"Model did not return JSON: {text[:300]!r}") from None
        try:
            raw = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMInvalidOutput(f"Invalid JSON from model: {exc}") from exc
    try:
        return output_type.model_validate(raw).model_dump(mode="json")
    except ValidationError as exc:
        raise LLMInvalidOutput(f"Output does not match schema: {exc}") from exc
