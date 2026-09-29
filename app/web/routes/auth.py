from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.models import User
from app.web.deps import db
from app.web.templating import flash, render

router = APIRouter()

MAX_FAILURES = 5
LOCKOUT_S = 300


class LoginThrottle:
    """In-memory brute-force protection: 5 failures per (IP, username) -> 5 minutes lockout."""

    def __init__(self) -> None:
        self._failures: dict[tuple[str, str], list[float]] = {}

    def _recent(self, key: tuple[str, str]) -> list[float]:
        now = time.monotonic()
        items = [t for t in self._failures.get(key, []) if now - t < LOCKOUT_S]
        self._failures[key] = items
        return items

    def blocked(self, key: tuple[str, str]) -> bool:
        return len(self._recent(key)) >= MAX_FAILURES

    def fail(self, key: tuple[str, str]) -> None:
        self._recent(key).append(time.monotonic())
        if len(self._failures) > 10_000:  # bound memory under a spray attack
            self._failures.clear()

    def reset(self, key: tuple[str, str]) -> None:
        self._failures.pop(key, None)


throttle = LoginThrottle()


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html", user=None)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          s: Session = Depends(db)):
    key = (request.client.host if request.client else "?", username.strip().lower())
    if throttle.blocked(key):
        flash(request, "Слишком много попыток. Подождите 5 минут.", "error")
        return RedirectResponse("/login", status_code=303)
    user = s.scalar(select(User).where(User.username == username.strip()))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        throttle.fail(key)
        flash(request, "Неверный логин или пароль", "error")
        return RedirectResponse("/login", status_code=303)
    throttle.reset(key)
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
