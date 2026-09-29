"""LLMTask: what to ask (prompt templates + output schema + version)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

COMMON_PROMPTS_DIR = Path(__file__).parent / "prompts"


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
        env = self._env()
        system = env.get_template(self.system_template).render(**variables).strip()
        user = env.get_template(self.user_template).render(**variables).strip()
        return system, user
