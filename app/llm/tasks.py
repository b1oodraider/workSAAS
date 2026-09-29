"""LLMTask: what to ask (prompt templates + output schema + version)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

COMMON_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Tags that delimit untrusted data in prompts (see prompts/_common.j2).
DATA_TAGS = ("resume", "vacancy", "preferences", "user_text", "match_analysis")
_TAG_RE = re.compile(r"<\s*(/?)\s*(" + "|".join(DATA_TAGS) + r")\b", re.I)


def neutralize(value: Any) -> Any:
    """Make untrusted text unable to close/open our data tags (e.g. '</vacancy>' inside a vacancy).

    '<' is replaced with a look-alike so the text stays readable for the model.
    """
    if isinstance(value, str):
        return _TAG_RE.sub(lambda m: "‹" + m.group(1) + m.group(2), value)
    if isinstance(value, dict):
        return {k: neutralize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(neutralize(v) for v in value)
    if isinstance(value, BaseModel):
        return value.model_copy(update={k: neutralize(getattr(value, k)) for k in type(value).model_fields})
    return value


@dataclass(frozen=True)
class LLMTask(Generic[T]):
    name: str
    # Bump whenever templates or schema change: invalidates the LLM cache.
    version: str
    output: type[T]
    template_dir: Path
    system_template: str = "system.j2"
    user_template: str = "user.j2"
    max_tokens: int = 16000
    cache_system: bool = False

    def _env(self) -> Environment:
        return Environment(
            loader=ChoiceLoader(
                [FileSystemLoader(self.template_dir), FileSystemLoader(COMMON_PROMPTS_DIR)]
            ),
            undefined=StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
        )

    def render(self, variables: dict[str, Any]) -> tuple[str, str]:
        variables = {k: neutralize(v) for k, v in variables.items()}
        env = self._env()
        system = env.get_template(self.system_template).render(**variables).strip()
        user = env.get_template(self.user_template).render(**variables).strip()
        return system, user
