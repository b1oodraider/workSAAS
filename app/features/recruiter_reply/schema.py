from __future__ import annotations

from pydantic import BaseModel, Field


class Answer(BaseModel):
    question: str = Field(description="Вопрос из сообщения рекрутера")
    answer: str = Field(description="Ответ на фактах из резюме и пожеланий; если данных нет — что кандидату нужно решить самому")


class RecruiterReply(BaseModel):
    understanding: str = Field(description="Что на самом деле хочет рекрутер, 1-2 предложения")
    answers: list[Answer] = Field(description="До 6")
    questions_to_clarify: list[str] = Field(description="До 4 вопросов, которые стоит задать в ответ")
    watch_out: list[str] = Field(description="До 4 вещей, которые не стоит говорить или на что обратить внимание")
    reply: str = Field(description="Готовый текст ответа рекрутеру")
