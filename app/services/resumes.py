from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.documents.extract import ExtractError, extract_text
from app.models import Resume
from app.services.errors import NotFound, ValidationFailed

MAX_RESUME_CHARS = 60_000


def get_owned(s: Session, user_id: int, resume_id: int) -> Resume:
    resume = s.get(Resume, resume_id)
    if resume is None or resume.user_id != user_id:
        raise NotFound("resume")
    return resume


def list_for_user(s: Session, user_id: int) -> list[Resume]:
    return list(s.scalars(select(Resume).where(Resume.user_id == user_id).order_by(Resume.id.desc())))


def create(
    s: Session,
    user_id: int,
    *,
    title: str,
    text: str = "",
    preferences: str = "",
    filename: str | None = None,
    file_data: bytes | None = None,
) -> Resume:
    if file_data:
        try:
            text = extract_text(filename or "resume.txt", file_data)
        except ExtractError as exc:
            raise ValidationFailed(str(exc)) from exc
    text = text.strip()
    if len(text) < 50:
        raise ValidationFailed("Резюме слишком короткое: вставьте текст или загрузите файл")
    if len(text) > MAX_RESUME_CHARS:
        raise ValidationFailed(f"Резюме длиннее {MAX_RESUME_CHARS} символов — сократите его")
    resume = Resume(
        user_id=user_id,
        title=title.strip() or (text.splitlines()[0][:100] if text else "Резюме"),
        text=text,
        preferences=preferences.strip(),
        source_filename=filename,
    )
    s.add(resume)
    s.flush()
    return resume


def update(s: Session, user_id: int, resume_id: int, *, title: str, text: str, preferences: str) -> Resume:
    resume = get_owned(s, user_id, resume_id)
    text = text.strip()
    if len(text) < 50:
        raise ValidationFailed("Резюме слишком короткое")
    resume.title, resume.text, resume.preferences = title.strip() or resume.title, text, preferences.strip()
    return resume


def delete(s: Session, user_id: int, resume_id: int) -> None:
    s.delete(get_owned(s, user_id, resume_id))
