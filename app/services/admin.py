"""Admin use-cases: user management and health of sources / LLM providers."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.db import month_start, utcnow
from app.core.security import hash_password
from app.models import Job, JobStatus, LLMUsage, User
from app.services.errors import NotFound, ValidationFailed
from app.sources.registry import SOURCE_CLASSES, source_config


def users_overview(s: Session) -> list[dict[str, Any]]:
    spent = dict(s.execute(
        select(LLMUsage.user_id, func.sum(LLMUsage.cost_usd))
        .where(LLMUsage.created_at >= month_start()).group_by(LLMUsage.user_id)
    ).all())
    users = s.scalars(select(User).order_by(User.id)).all()
    return [{"user": u, "spent": float(spent.get(u.id) or 0.0)} for u in users]


def create_user(s: Session, username: str, *, password: str = "", is_admin: bool = False,
                budget: float | None = None) -> tuple[User, str]:
    username = username.strip()
    if not username or len(username) > 64:
        raise ValidationFailed("Логин: от 1 до 64 символов")
    if s.scalar(select(User).where(User.username == username)):
        raise ValidationFailed(f"Пользователь {username} уже есть")
    password = password or secrets.token_urlsafe(9)
    if len(password) < 8:
        raise ValidationFailed("Пароль — минимум 8 символов")
    user = User(username=username, password_hash=hash_password(password), is_admin=is_admin,
                monthly_budget_usd=budget)
    s.add(user)
    s.flush()
    return user, password


def _get(s: Session, user_id: int) -> User:
    user = s.get(User, user_id)
    if user is None:
        raise NotFound("user")
    return user


def set_budget(s: Session, user_id: int, budget: float | None) -> None:
    _get(s, user_id).monthly_budget_usd = budget


def set_active(s: Session, actor_id: int, user_id: int, active: bool) -> None:
    if actor_id == user_id and not active:
        raise ValidationFailed("Нельзя заблокировать самого себя")
    _get(s, user_id).is_active = active
    if not active:
        # A blocked user's saved job-site logins must not stay usable on the server.
        from app.services import autoapply as autoapply_svc

        autoapply_svc.forget_all_sessions(user_id)


def reset_password(s: Session, user_id: int) -> str:
    password = secrets.token_urlsafe(9)
    _get(s, user_id).password_hash = hash_password(password)
    return password


def sources_health(s: Session, days: int = 7) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Per source: enabled state and errors reported by recent search runs."""
    since = utcnow() - timedelta(days=days)
    jobs = s.scalars(
        select(Job).where(Job.kind == "search_run", Job.created_at >= since).order_by(Job.id.desc()).limit(200)
    ).all()
    rows = []
    for name, cls in SOURCE_CLASSES.items():
        if not cls.searchable:
            continue
        errors = [e for j in jobs for e in ((j.result or {}).get("errors") or [])
                  if isinstance(e, str) and e.startswith(cls.title)]
        rows.append({
            "name": name, "title": cls.title, "enabled": source_config(name).enabled,
            "errors": len(errors), "last_error": errors[0] if errors else "",
        })
    failed_runs = sum(1 for j in jobs if j.status == JobStatus.failed)
    return rows, {"runs": len(jobs), "failed_runs": failed_runs}


def llm_health(s: Session, days: int = 7) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(days=days)
    rows = s.execute(
        select(LLMUsage.provider, LLMUsage.model, func.count(),
               func.sum(case((LLMUsage.ok.is_(False), 1), else_=0)),
               func.avg(LLMUsage.latency_ms), func.sum(LLMUsage.cost_usd))
        .where(LLMUsage.created_at >= since, LLMUsage.cached.is_(False))
        .group_by(LLMUsage.provider, LLMUsage.model)
    ).all()
    return [{"provider": p, "model": m, "calls": n, "failed": int(f or 0),
             "avg_latency_s": round((lat or 0) / 1000, 1), "cost": float(c or 0)}
            for p, m, n, f, lat, c in rows]
