from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

ApplyStatus = Literal["applied", "skipped", "failed", "blocked"]


@dataclass
class ApplyRequest:
    user_id: int
    source: str
    external_id: str
    url: str | None
    letter: str
    # Which of the user's resumes on the site to use (substring of its title); "" = site default.
    site_resume_title: str = ""


@dataclass
class ApplyResult:
    # applied  — the site confirmed the application
    # skipped  — nothing sent on purpose (questionnaire required, already applied, vacancy closed)
    # failed   — something went wrong with this vacancy; others may still work
    # blocked  — stop everything for this user (captcha, logged out, account restrictions)
    status: ApplyStatus
    reason: str = ""


class Applier(ABC):
    source: str = ""
    title: str = ""

    @abstractmethod
    def is_ready(self, user_id: int) -> tuple[bool, str]:
        """Can this user apply at all (e.g. logged-in session saved)? Returns (ok, hint)."""

    @abstractmethod
    async def apply(self, req: ApplyRequest) -> ApplyResult: ...
