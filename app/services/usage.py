"""LLM spending as shown to users (web «Расходы», bot /usage, admin)."""

from __future__ import annotations

from app.llm import get_gateway


def spent(user_id: int) -> float:
    return get_gateway().month_spent(user_id)


def budget(user_id: int) -> float:
    """Monthly limit in USD; 0 or less means unlimited."""
    return get_gateway().budget_for(user_id)
