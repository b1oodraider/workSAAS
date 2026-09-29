"""AnalysisFeature: declarative description of one LLM-powered analysis.

The generic runner (app.services.analysis) does the rest: loads subjects,
renders prompts, calls the gateway, stores the Analysis, and the web layer
renders buttons, parameter forms and results for every registered feature.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from app.llm.tasks import LLMTask

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import Resume, Vacancy

Subject = Literal["resume", "vacancy", "resume_vacancy"]


class NoParams(BaseModel):
    pass


# (session, user_id, resume|None, vacancy|None, params) -> extra template variables
ContextBuilder = Callable[
    ["Session", int, "Resume | None", "Vacancy | None", BaseModel], dict[str, Any]
]


@dataclass(frozen=True)
class AnalysisFeature:
    kind: str
    title: str
    description: str
    subject: Subject
    task: LLMTask
    params_model: type[BaseModel] = NoParams
    # Name of an int/float field in the output copied to Analysis.score (sortable).
    score_field: str | None = None
    context_builder: ContextBuilder | None = None
    # Hidden features run internally (e.g. resume_profile) and get no UI button.
    hidden: bool = False
    order: int = 100

    @property
    def view_template(self) -> str:
        """Template path relative to app/features (see web template loader)."""
        return f"{self.task.template_dir.name}/view.html"

    @property
    def needs_resume(self) -> bool:
        return self.subject in ("resume", "resume_vacancy")

    @property
    def needs_vacancy(self) -> bool:
        return self.subject in ("vacancy", "resume_vacancy")


def feature_dir(file: str) -> Path:
    return Path(file).parent
