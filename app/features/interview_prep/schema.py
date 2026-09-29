from __future__ import annotations

from pydantic import BaseModel, Field


class LikelyQuestion(BaseModel):
    question: str
    why_they_ask: str = Field(description="Что на самом деле проверяют этим вопросом")
    answer_outline: str = Field(description="План ответа по STAR, только на фактах из резюме; если фактов нет — так и скажи")


class Topic(BaseModel):
    topic: str
    what_to_review: str = Field(description="Что конкретно повторить")


class WeakSpot(BaseModel):
    gap: str = Field(description="Пробел кандидата относительно вакансии")
    how_to_answer: str = Field(description="Как честно отвечать, если спросят")


class InterviewPrep(BaseModel):
    summary: str = Field(description="2-3 предложения: на чём будет фокус интервью и главная стратегия")
    likely_questions: list[LikelyQuestion] = Field(description="До 8, самые вероятные сначала")
    technical_topics: list[Topic] = Field(description="До 6 тем, которые стоит повторить")
    weak_spots: list[WeakSpot] = Field(description="До 4")
    questions_to_employer: list[str] = Field(description="До 6 сильных вопросов работодателю")
    salary_talk: str = Field(description="Как говорить о зарплате: опирайся на вилку вакансии и пожелания кандидата, без выдуманных рыночных цифр")
