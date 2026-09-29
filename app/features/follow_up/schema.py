from __future__ import annotations

from pydantic import BaseModel, Field


class FollowUp(BaseModel):
    subject: str = Field(description="Тема для email; для чата hh.ru/Telegram — короткая первая строка")
    body: str = Field(description="Короткое сообщение: 300-700 знаков")
    when_to_send: str = Field(description="Когда и через какой канал лучше отправить")
    tips: list[str] = Field(description="До 3 советов по ситуации")
