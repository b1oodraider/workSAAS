"""Errors whose message is meant for the user (shown in the UI / bot as is).

Everything the job worker should report instead of treating as a crash derives
from UserError: validation, not found, source and LLM failures, job errors.
"""

from __future__ import annotations


class UserError(Exception):
    # Temporary problems (network, rate limits) may be retried by the job queue.
    retryable = False


class NotFound(UserError):
    """Entity doesn't exist or doesn't belong to the user (never leak which)."""

    def __str__(self) -> str:
        return "Не найдено"


class ValidationFailed(UserError):
    """User input is invalid."""
