from __future__ import annotations

from pydantic import BaseModel, Field


class CoverLetter(BaseModel):
    # Facts first: the model picks what it can back up from the resume, then writes the letter.
    key_points_used: list[str] = Field(
        description="До 4 фактов из резюме, которые войдут в письмо, с источником: «место работы/проект — факт»"
    )
    subject: str = Field(description="Тема для email (в отклике на hh.ru не используется)")
    body: str = Field(description="Готовый текст сопроводительного письма")
    warnings: list[str] = Field(
        description="До 4: что проверить или дописать самому перед отправкой (пробелы, неотвеченные вопросы "
                    "работодателя, сомнительные места)"
    )
