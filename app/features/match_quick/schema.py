from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, computed_field


class QuickMatch(BaseModel):
    # Reason first: models without hidden thinking commit to a number last.
    summary: str = Field(description="Одно предложение до 200 знаков: главное требование вакансии, "
                                     "которое кандидат закрывает или не закрывает")
    score: int = Field(ge=0, le=100, description="0-100 по шкале из инструкции")

    @computed_field  # derived, not asked from the model: always consistent with the score
    @property
    def verdict(self) -> Literal["strong", "good", "stretch", "weak", "mismatch"]:
        s = self.score
        return ("strong" if s >= 85 else "good" if s >= 70 else "stretch" if s >= 50
                else "weak" if s >= 30 else "mismatch")
