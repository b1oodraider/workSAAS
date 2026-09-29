"""Linking Telegram chats to web accounts with one-time codes."""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models import Resume, TelegramLinkCode, User, UserVacancy

CODE_TTL = timedelta(minutes=15)


def create_code(s: Session, user_id: int) -> str:
    s.execute(delete(TelegramLinkCode).where(TelegramLinkCode.user_id == user_id))
    code = secrets.token_urlsafe(12)  # ~96 bits, fits Telegram's 64-char start parameter
    s.add(TelegramLinkCode(code=code, user_id=user_id, expires_at=utcnow() + CODE_TTL))
    s.flush()
    return code


def link_chat(s: Session, code: str, chat_id: int, username: str | None) -> User | None:
    """Consume the code and bind the chat. Returns the user or None if the code is invalid."""
    row = s.get(TelegramLinkCode, code.strip())
    if row is None:
        return None
    s.delete(row)  # single use, also when expired
    if row.expires_at < utcnow():
        return None
    # One chat -> one account.
    for other in s.scalars(select(User).where(User.telegram_chat_id == chat_id)):
        other.telegram_chat_id = None
    user = s.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    user.telegram_chat_id = chat_id
    user.telegram_username = (username or "")[:64] or None
    # Start notifications from now on instead of flooding the chat with old matches.
    s.execute(update(UserVacancy).where(UserVacancy.user_id == user.id, UserVacancy.notified_at.is_(None))
              .values(notified_at=utcnow()))
    s.flush()
    return user


def unlink(s: Session, user_id: int) -> None:
    user = s.get(User, user_id)
    if user:
        user.telegram_chat_id = None
        user.telegram_username = None


def user_for_chat(s: Session, chat_id: int) -> User | None:
    user = s.scalar(select(User).where(User.telegram_chat_id == chat_id))
    return user if user and user.is_active else None


def default_resume(s: Session, user: User) -> Resume | None:
    if user.default_resume_id:
        resume = s.get(Resume, user.default_resume_id)
        if resume and resume.user_id == user.id:
            return resume
    return s.scalar(select(Resume).where(Resume.user_id == user.id).order_by(Resume.id.desc()).limit(1))
