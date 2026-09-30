from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from app.apply import sessions

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
    # failed only: a network/site hiccup before anything was submitted — worth another try later.
    transient: bool = False
    # failed/blocked after pressing submit: the application may have gone out; count it against limits.
    maybe_sent: bool = False


class Applier(ABC):
    """Sends an application to one job site on behalf of a user (one call = one application)."""

    source: str = ""                          # JobSource.name of the vacancies it handles
    title: str = ""                           # shown to users
    login_url: str = ""                       # opened by `worksaas site-login`
    account_url: str = ""                     # a page that only opens when logged in (login check)
    session_domains: tuple[str, ...] = ()     # cookies kept from the user's browser session

    @abstractmethod
    def is_ready(self, user_id: int) -> tuple[bool, str]:
        """Can this user apply at all (e.g. logged-in session saved)? Returns (ok, hint)."""

    @abstractmethod
    async def apply(self, req: ApplyRequest) -> ApplyResult: ...

    # Saved login sessions (see app/apply/sessions.py).

    def has_session(self, user_id: int) -> bool:
        return sessions.session_path(self.source, user_id).is_file()

    def clean_session(self, raw: Any) -> dict[str, Any]:
        return sessions.clean_state(raw, self.session_domains, self.title)

    def save_session(self, user_id: int, raw: Any) -> None:
        sessions.write_state(sessions.session_path(self.source, user_id), self.clean_session(raw))

    def save_session_bytes(self, user_id: int, data: bytes) -> None:
        self.save_session(user_id, sessions.parse_bytes(data))

    def load_session(self, user_id: int) -> dict[str, Any] | None:
        return sessions.read_state(sessions.session_path(self.source, user_id))

    def delete_session(self, user_id: int) -> None:
        sessions.session_path(self.source, user_id).unlink(missing_ok=True)
