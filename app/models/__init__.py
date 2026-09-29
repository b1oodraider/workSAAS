"""ORM models. Import everything here so Base.metadata is complete."""

from app.models.analysis import Analysis
from app.models.job import Job, JobStatus
from app.models.llm import LLMCache, LLMUsage
from app.models.resume import Resume
from app.models.search import SavedSearch
from app.models.user import TelegramLinkCode, User
from app.models.vacancy import UserVacancy, UserVacancyStatus, Vacancy

__all__ = [
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
