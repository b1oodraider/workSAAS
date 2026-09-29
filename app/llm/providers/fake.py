"""Offline provider: returns schema-valid placeholder data. For tests and UI development."""

from __future__ import annotations

from typing import Any, ClassVar

from app.llm.base import LLMRequest, LLMResponse, TokenUsage


def _sample(schema: dict[str, Any], defs: dict[str, Any], name: str = "") -> Any:
    if "$ref" in schema:
        return _sample(defs[schema["$ref"].split("/")[-1]], defs, name)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [s for s in schema[key] if s.get("type") != "null"] or schema[key]
            return _sample(options[0], defs, name)
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties", {})
        return {k: _sample(v, defs, k) for k, v in props.items()}
    if kind == "array":
        return [_sample(schema.get("items", {}), defs, name) for _ in range(2)]
    if kind == "integer":
        lo, hi = schema.get("minimum", 0), schema.get("maximum", 100)
        return (lo + hi) // 2
    if kind == "number":
        lo, hi = schema.get("minimum", 0), schema.get("maximum", 100)
        return (lo + hi) / 2
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return f"[fake {name}]"


class FakeProvider:
    # Tests can pin exact outputs per task name.
    canned: ClassVar[dict[str, dict[str, Any]]] = {}
    calls: ClassVar[list[LLMRequest]] = []

    def __init__(self, name: str = "fake") -> None:
        self.name = name

    async def generate(self, req: LLMRequest) -> LLMResponse:
        FakeProvider.calls.append(req)
        if req.task in self.canned:
            data = self.canned[req.task]
        else:
            schema = req.json_schema
            data = _sample(schema, schema.get("$defs", {}))
        data = req.output_type.model_validate(data).model_dump(mode="json")
        return LLMResponse(
            data=data,
            model=req.model,
            usage=TokenUsage(input_tokens=len(req.system + req.user) // 4, output_tokens=100),
        )
