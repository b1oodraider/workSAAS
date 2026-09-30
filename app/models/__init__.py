"""ORM models. Import everything here so Base.metadata is complete."""

from app.models.analysis import Analysis
from app.models.application import (
    ACTIVE_APPLICATION_STATUSES,
    Application,
    ApplicationStatus,
    AutoApplySettings,
)
from app.models.job import Job, JobStatus
from app.models.llm import LLMCache, LLMUsage
from app.models.resume import Resume
from app.models.search import SavedSearch
from app.models.user import TelegramLinkCode, User
from app.models.vacancy import (
    MATCH_VOTE_REASONS,
    RESPONSE_QUALITY,
    STATUS_LABELS,
    UserVacancy,
    UserVacancyStatus,
    Vacancy,
)

__all__ = [
    "ACTIVE_APPLICATION_STATUSES",
    "Application",
    "ApplicationStatus",
    "AutoApplySettings",
    "MATCH_VOTE_REASONS",
    "RESPONSE_QUALITY",
    "STATUS_LABELS",
    "Analysis",
    "Job",
    "JobStatus",
    "LLMCache",
    "LLMUsage",
    "Resume",
    "SavedSearch",
    "TelegramLinkCode",
    "User",
    "UserVacancy",
    "UserVacancyStatus",
    "Vacancy",
]
