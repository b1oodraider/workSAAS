"""LLMGateway: routing, prompt rendering, caching, budget and usage accounting.

Every LLM call in the app goes through ``LLMGateway.run``. Features never talk
to providers directly, so swapping models or adding providers is config-only.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from app.core.config import LLMTarget, Settings, get_settings
from app.core.db import month_start, session_scope
from app.core.text import stable_hash
from app.llm.base import (
    BudgetExceeded,
    LLMError,
    LLMInvalidOutput,
    LLMProvider,
    LLMRefusal,
    LLMRequest,
    TokenUsage,
)
from app.llm.pricing import cost_usd
from app.llm.providers import build_provider
from app.llm.tasks import LLMTask
from app.models import LLMCache, LLMUsage, User

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass
class TaskResult(Generic[T]):
    output: T
    provider: str
    model: str
    prompt_version: str
    cached: bool
    cost_usd: float


class LLMGateway:
    def __init__(
        self, settings: Settings | None = None, providers: dict[str, LLMProvider] | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self._providers: dict[str, LLMProvider] = dict(providers or {})

    def provider(self, name: str) -> LLMProvider:
        if name not in self._providers:
            cfg = self.settings.llm.providers.get(name)
            if cfg is None:
                raise LLMError(f"LLM provider {name!r} is not configured")
            self._providers[name] = build_provider(name, cfg)
        return self._providers[name]

    # --- budget -------------------------------------------------------------

    def month_spent(self, user_id: int) -> float:
        with session_scope() as s:
            total = s.scalar(
                select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0)).where(
                    LLMUsage.user_id == user_id, LLMUsage.created_at >= month_start()
                )
            )
        return float(total or 0.0)

    def budget_for(self, user_id: int) -> float:
        with session_scope() as s:
            user = s.get(User, user_id)
            custom = user.monthly_budget_usd if user else None
        return custom if custom is not None else self.settings.default_monthly_budget_usd

    def _check_budget(self, user_id: int | None) -> None:
        if user_id is None:
            return
        budget = self.budget_for(user_id)
        if budget <= 0:  # 0 or negative = unlimited
            return
        spent = self.month_spent(user_id)
        if spent >= budget:
            raise BudgetExceeded(
                f"Месячный бюджет на LLM исчерпан: потрачено ${spent:.2f} из ${budget:.2f}"
            )

    # --- main entry point ---------------------------------------------------

    async def run(
        self,
        task: LLMTask[T],
        variables: dict[str, Any],
        *,
        user_id: int | None,
        use_cache: bool = True,
    ) -> TaskResult[T]:
        route = self.settings.llm.route_for(task.name)
        variables = {"language": self.settings.output_language, **variables}
        system, user = task.render(variables)
        use_cache = use_cache and self.settings.llm.cache_enabled
        targets = route.targets()

        # Any target's cached answer is good enough (e.g. produced while the primary was down).
        if use_cache:
            for target in targets:
                hit = self._cache_get(self._cache_key(task, target, system, user))
                if hit is not None:
                    self._record(user_id, task.name, target.provider, target.model, TokenUsage(), 0.0,
                                 cached=True, ok=True, latency_ms=0)
                    return TaskResult(output=task.output.model_validate(hit), provider=target.provider,
                                      model=target.model, prompt_version=task.version, cached=True,
                                      cost_usd=0.0)

        self._check_budget(user_id)
        last_error: LLMError | None = None
        for i, target in enumerate(targets):
            try:
                result = await self._call(task, target, system, user, user_id)
            except LLMRefusal:
                raise  # another provider is not a fix for a policy decline
            except LLMError as exc:
                last_error = exc
                if i + 1 < len(targets):
                    log.warning("llm %s via %s failed (%s), trying fallback %s", task.name,
                                target.provider, exc, targets[i + 1].provider)
                continue
            if use_cache:
                with session_scope() as s:
                    s.merge(LLMCache(key=self._cache_key(task, target, system, user), task=task.name,
                                     provider=target.provider, model=target.model,
                                     output=result.output.model_dump(mode="json")))
            return result
        assert last_error is not None
        raise last_error

    @staticmethod
    def _cache_key(task: LLMTask, target: LLMTarget, system: str, user: str) -> str:
        return stable_hash(task.name, task.version, target.provider, target.model, target.effort, system, user)

    @staticmethod
    def _cache_get(key: str) -> dict[str, Any] | None:
        with session_scope() as s:
            hit = s.get(LLMCache, key)
            return dict(hit.output) if hit else None

    async def _call(self, task: LLMTask[T], target: LLMTarget, system: str, user: str,
                    user_id: int | None) -> TaskResult[T]:
        provider = self.provider(target.provider)
        req = LLMRequest(
            task=task.name,
            model=target.model,
            system=system,
            user=user,
            output_type=task.output,
            max_tokens=target.max_tokens or task.max_tokens,
            effort=target.effort,
            cache_system=task.cache_system,
        )
        started = time.monotonic()
        try:
            resp = await provider.generate(req)
            output = task.output.model_validate(resp.data)
        except ValidationError as exc:
            self._record(user_id, task.name, target.provider, target.model, TokenUsage(), 0.0,
                         cached=False, ok=False, latency_ms=int((time.monotonic() - started) * 1000))
            raise LLMInvalidOutput(f"Output does not match schema: {exc}") from exc
        except LLMError:
            self._record(user_id, task.name, target.provider, target.model, TokenUsage(), 0.0,
                         cached=False, ok=False, latency_ms=int((time.monotonic() - started) * 1000))
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        cfg = self.settings.llm.providers.get(target.provider)
        multiplier = cfg.price_multiplier if cfg else 1.0
        cost = round(cost_usd(target.model, resp.usage) * multiplier, 6)
        self._record(user_id, task.name, target.provider, resp.model, resp.usage, cost,
                     cached=False, ok=True, latency_ms=latency_ms)
        log.info("llm task=%s provider=%s model=%s cost=$%.4f latency=%dms", task.name, target.provider,
                 resp.model, cost, latency_ms)
        return TaskResult(output=output, provider=target.provider, model=resp.model,
                          prompt_version=task.version, cached=False, cost_usd=cost)

    def _record(
        self,
        user_id: int | None,
        task: str,
        provider: str,
        model: str,
        usage: TokenUsage,
        cost: float,
        *,
        cached: bool,
        ok: bool,
        latency_ms: int,
    ) -> None:
        with session_scope() as s:
            s.add(
                LLMUsage(
                    user_id=user_id,
                    task=task,
                    provider=provider,
                    model=model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    cost_usd=cost,
                    cached=cached,
                    ok=ok,
                    latency_ms=latency_ms,
                )
            )


_gateway: LLMGateway | None = None


def get_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
    return _gateway


def set_gateway(gateway: LLMGateway | None) -> None:
    """Tests inject a gateway with fake providers."""
    global _gateway
    _gateway = gateway
