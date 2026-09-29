from __future__ import annotations

from pydantic import BaseModel, Field


class CoverLetter(BaseModel):
    subject: str = Field(description="Тема письма / первая строка отклика")
    body: str = Field(description="Готовый текст сопроводительного письма")
    key_points_used: list[str] = Field(description="Какие факты из резюме использованы")
    warnings: list[str] = Field(
        description="О чём кандидату стоит знать перед отправкой: слабые места, что проверить/дописать самому"
    )
