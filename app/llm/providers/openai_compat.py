"""Any OpenAI-compatible chat completions server: OpenRouter, DeepSeek, Ollama, LM Studio, vLLM."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

import httpx

from app.core.config import ProviderConfig
from app.core.http import make_async_client
from app.llm.base import (
    EmbeddingResponse,
    LLMError,
    LLMInvalidOutput,
    LLMRequest,
    LLMResponse,
    LLMUnavailable,
    TokenUsage,
    parse_json_output,
)

log = logging.getLogger(__name__)

# Request timeout and rate limit: the request was not processed, a repeat is safe.
_RETRY_STATUSES = frozenset({408, 429})


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
        if self.cfg.json_mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": req.task, "schema": req.inline_json_schema},
            }
        elif self.cfg.json_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if self.cfg.send_reasoning_effort and req.effort:
            # OpenAI-style APIs know low/medium/high only.
            payload["reasoning_effort"] = "high" if req.effort in ("xhigh", "max") else req.effort
        return payload

    async def generate(self, req: LLMRequest) -> LLMResponse:
        return self._parse(await self._post("/chat/completions", self._payload(req)), req)

    async def embed(self, model: str, texts: list[str]) -> EmbeddingResponse:
        body = await self._post("/embeddings", {"model": model, "input": texts})
        try:
            items = sorted(body["data"], key=lambda d: d.get("index", 0))
            vectors = [[float(x) for x in item["embedding"]] for item in items]
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            err = LLMInvalidOutput(f"{self.name}: unexpected embeddings response")
            err.detail = str(body)[:300]
            log.warning("%s: unexpected embeddings response: %s", self.name, err.detail)
            raise err from exc
        if len(vectors) != len(texts) or any(not v or not all(map(math.isfinite, v)) for v in vectors):
            raise LLMInvalidOutput(f"{self.name}: got {len(vectors)} vectors for {len(texts)} texts")
        raw_usage = body.get("usage")
        raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
        usage = TokenUsage(input_tokens=raw_usage.get("prompt_tokens", 0) or 0)
        return EmbeddingResponse(vectors=vectors, model=body.get("model") or model, usage=usage)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with retries on connection errors, 408, 429 and 5xx; returns the JSON body.

        A read timeout is not retried here: the server may have finished (and billed) the answer,
        so a blind repeat could pay twice. It raises LLMUnavailable and the job decides."""
        scheme = self.cfg.auth_scheme or "Bearer"
        headers = {"Authorization": f"{scheme} {self.cfg.api_key}"} if self.cfg.api_key else {}
        headers.update(self.cfg.resolved_headers())
        last_exc: Exception | None = None
        async with make_async_client(
            use_proxy=self.cfg.use_proxy, timeout=self.cfg.timeout_s, headers=headers
        ) as client:
            for attempt in range(self.cfg.max_retries + 1):
                if attempt:
                    await asyncio.sleep(2 ** (attempt - 1))  # 1, 2, 4… s; none after the last try
                try:
                    resp = await client.post(f"{self.base_url}{path}", json=payload)
                except httpx.ReadTimeout as exc:
                    raise LLMUnavailable(f"{self.name}: no answer within {self.cfg.timeout_s:g} s") from exc
                except httpx.HTTPError as exc:
                    last_exc = exc
                else:
                    if resp.status_code in _RETRY_STATUSES or resp.status_code >= 500:
                        last_exc = LLMUnavailable(f"{self.name}: HTTP {resp.status_code}")
                    elif resp.status_code >= 400:
                        # The body may echo the request or carry provider internals: logs only.
                        detail = resp.text[:500]
                        log.warning("%s %s: HTTP %s: %s", self.name, path, resp.status_code, detail)
                        err = LLMError(f"{self.name}: HTTP {resp.status_code}")
                        err.detail = detail
                        raise err
                    else:
                        try:
                            body = resp.json()
                        except ValueError:  # proxy/reseller glitch: HTML or empty body with 200
                            last_exc = LLMUnavailable(f"{self.name}: non-JSON response")
                        else:
                            if isinstance(body, dict):
                                return body
                            last_exc = LLMUnavailable(f"{self.name}: unexpected JSON ({type(body).__name__})")
        raise LLMUnavailable(f"{self.name} unavailable: {last_exc}")

    def _parse(self, body: dict[str, Any], req: LLMRequest) -> LLMResponse:
        try:
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            err = LLMInvalidOutput(f"{self.name}: unexpected response shape")
            err.detail = str(body)[:300]
            log.warning("%s: unexpected response shape: %s", self.name, err.detail)
            raise err from exc
        raw_usage = body.get("usage")
        raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
        usage = TokenUsage(
            input_tokens=raw_usage.get("prompt_tokens", 0) or 0,
            output_tokens=raw_usage.get("completion_tokens", 0) or 0,
        )
        try:
            if choice.get("finish_reason") == "length":
                raise LLMInvalidOutput("Response truncated by max_tokens")
            data = parse_json_output(text, req.output_type)
        except LLMInvalidOutput as exc:
            exc.usage = usage
            raise
        return LLMResponse(data=data, model=body.get("model") or req.model, usage=usage)
