from __future__ import annotations

from pydantic import BaseModel, Field


class CoverLetter(BaseModel):
    # Facts first: models without hidden reasoning pick them before writing the body.
    # Optional: a model that skips it must not cost a whole letter.
    key_points_used: list[str] = Field(
        default_factory=list,
        description="3-5 фактов из резюме почти дословно, на которые опирается письмо; в body — только они")
    subject: str = Field(description="Тема письма / первая строка отклика")
    body: str = Field(description="Готовый текст сопроводительного письма")
    warnings: list[str] = Field(
        description="0-3 пункта, каждый до 150 знаков, обращение к кандидату на «вы»: что проверить или дописать "
                    "перед отправкой (пробел под требование, факт, который стоит добавить в резюме). "
                    "Не пиши, как ты соблюдал правила (что не выдумывал, как назвал проект), и не комментируй даты. "
                    "Нечего сказать — пустой список."
    )
