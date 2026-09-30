"""Any OpenAI-compatible chat completions server: OpenRouter, DeepSeek, Ollama, LM Studio, vLLM."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.core.config import ProviderConfig
from app.core.http import make_async_client
from app.llm.base import (
    LLMError,
    LLMInvalidOutput,
    LLMRequest,
    LLMResponse,
    LLMTruncated,
    LLMUnavailable,
    TokenUsage,
    parse_json_output,
)


# Our effort scale -> OpenAI ``reasoning_effort``; most servers stop at "high".
_REASONING_EFFORT = {"none": "none", "low": "low", "medium": "medium", "high": "high",
                     "xhigh": "high", "max": "high"}


class OpenAICompatProvider:
    def __init__(self, name: str, cfg: ProviderConfig) -> None:
        if not cfg.base_url:
            raise LLMError(f"Provider {name!r}: base_url is required for openai_compat")
        self.name = name
        self.cfg = cfg
        self.base_url = cfg.base_url.rstrip("/")

    def _payload(self, req: LLMRequest) -> dict[str, Any]:
        system = req.system
        if self.cfg.json_mode != "json_schema":
            # Without native schema support the schema must be in the prompt.
            system += (
                "\n\nВерни ТОЛЬКО JSON-объект, соответствующий этой JSON Schema, без пояснений:\n"
                + json.dumps(req.inline_json_schema, ensure_ascii=False)
            )
        payload: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": req.user},
            ],
        }
        if req.effort and self.cfg.reasoning_effort:
            payload["reasoning_effort"] = _REASONING_EFFORT[req.effort]
        if self.cfg.json_mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": req.task, "schema": req.inline_json_schema},
            }
        elif self.cfg.json_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def generate(self, req: LLMRequest) -> LLMResponse:
        scheme = self.cfg.auth_scheme or "Bearer"
        headers = {"Authorization": f"{scheme} {self.cfg.api_key}"} if self.cfg.api_key else {}
        headers.update(self.cfg.resolved_headers())
        payload = self._payload(req)
        last_exc: Exception | None = None
        async with make_async_client(
            use_proxy=self.cfg.use_proxy, timeout=self.cfg.timeout_s, headers=headers
        ) as client:
            for attempt in range(self.cfg.max_retries + 1):
                try:
                    resp = await client.post(f"{self.base_url}/chat/completions", json=payload)
                except httpx.HTTPError as exc:
                    last_exc = exc
                else:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        last_exc = LLMUnavailable(f"{self.name}: HTTP {resp.status_code}")
                    elif resp.status_code >= 400:
                        raise LLMError(f"{self.name}: HTTP {resp.status_code}: {resp.text[:500]}")
                    else:
                        return self._parse(resp.json(), req)
                await asyncio.sleep(2**attempt)
        raise LLMUnavailable(f"{self.name} unavailable: {last_exc}")

    def _parse(self, body: dict[str, Any], req: LLMRequest) -> LLMResponse:
        try:
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMInvalidOutput(f"Unexpected response shape: {str(body)[:300]}") from exc
        if choice.get("finish_reason") == "length":
            raise LLMTruncated("Response truncated by max_tokens")
        usage = body.get("usage") or {}
        return LLMResponse(
            data=parse_json_output(text, req.output_type),
            model=body.get("model") or req.model,
            usage=TokenUsage(
                input_tokens=usage.get("prompt_tokens", 0) or 0,
                output_tokens=usage.get("completion_tokens", 0) or 0,
            ),
        )
