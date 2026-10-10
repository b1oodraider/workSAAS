from __future__ import annotations

import secrets
import threading
import time
from functools import lru_cache

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password, session_stamp, verify_password
from app.models import User
from app.web.deps import db
from app.web.templating import flash, render

router = APIRouter()

# 5 failures per (IP, username) -> 5 minutes lockout.
MAX_FAILURES = 5
LOCKOUT_S = 300
# Per username from any IP (an attacker rotating proxies): 10 failures in 15 minutes.
USER_MAX_FAILURES = 10
USER_LOCKOUT_S = 900
# All failed logins together (password spraying over many usernames): 30 per minute.
GLOBAL_MAX_FAILURES = 30
GLOBAL_WINDOW_S = 60
MAX_USERNAME_KEY = 150  # usernames are short; don't keep megabytes of junk as keys
# "Known device": a signed cookie with the usernames that logged in from this browser. It exempts
# them from the per-username and global windows, so an attacker failing on purpose cannot lock the
# real user (or everybody) out; the per-(IP, username) window still applies.
KNOWN_DEVICE_COOKIE = "known_device"
KNOWN_DEVICE_S = 365 * 24 * 3600
KNOWN_DEVICE_MAX_USERS = 5


class _FailureWindow:
    """Failure timestamps per key within a sliding window."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window_s = limit, window_s
        self._failures: dict[object, list[float]] = {}
        self._lock = threading.Lock()  # sync handlers run in a thread pool

    def _recent(self, key: object) -> list[float]:
        now = time.monotonic()
        items = [t for t in self._failures.get(key, []) if now - t < self.window_s]
        self._failures[key] = items
        return items

    def blocked(self, key: object) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.limit

    def fail(self, key: object) -> None:
        with self._lock:
            self._recent(key).append(time.monotonic())
            if len(self._failures) > 10_000:  # bound memory under a spray attack
                # Keep the most-attacked keys: clearing everything would reset real victims' counters.
                keep = sorted(self._failures, key=lambda k: len(self._recent(k)), reverse=True)[:5_000]
                self._failures = {k: self._failures[k] for k in keep if self._failures[k]}

    def reset(self, key: object) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()


class LoginThrottle:
    """In-memory brute-force protection, per (IP, username), per username and for all logins."""

    def __init__(self) -> None:
        self.pair = _FailureWindow(MAX_FAILURES, LOCKOUT_S)
        self.user = _FailureWindow(USER_MAX_FAILURES, USER_LOCKOUT_S)
        self.total = _FailureWindow(GLOBAL_MAX_FAILURES, GLOBAL_WINDOW_S)

    def blocked(self, ip: str, username: str, *, known_device: bool = False) -> str | None:
        """Why logging in is refused right now (message for the user), or None. A browser this
        username already logged in from is only limited per (IP, username)."""
        if not known_device and self.total.blocked("*"):
            return "Слишком много попыток входа на сервер. Подождите минуту."
        if self.pair.blocked((ip, username)):
            return "Слишком много попыток. Подождите 5 минут."
        if not known_device and self.user.blocked(username):
            return "Слишком много попыток для этого логина. Подождите 15 минут."
        return None

    def fail(self, ip: str, username: str) -> None:
        self.total.fail("*")
        self.pair.fail((ip, username))
        self.user.fail(username)

    def success(self, ip: str, username: str) -> None:
        # The per-username counter is not reset: an attacker on other IPs keeps their count.
        self.pair.reset((ip, username))

    def clear(self) -> None:
        for window in (self.pair, self.user, self.total):
            window.clear()


throttle = LoginThrottle()


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def _device_serializer(request: Request) -> URLSafeTimedSerializer:
    # The key the session cookie is signed with (create_app may have generated a random one).
    return URLSafeTimedSerializer(request.app.state.secret_key, salt="known-device")


def known_device_users(request: Request) -> list[str]:
    """Usernames (login keys) that logged in from this browser; empty if the cookie is absent,
    expired or forged."""
    raw = request.cookies.get(KNOWN_DEVICE_COOKIE)
    if not raw:
        return []
    try:
        users = _device_serializer(request).loads(raw, max_age=KNOWN_DEVICE_S)
    except BadSignature:  # also SignatureExpired
        return []
    if not isinstance(users, list):
        return []
    return [u for u in users if isinstance(u, str)][:KNOWN_DEVICE_MAX_USERS]


def _remember_device(request: Request, response: RedirectResponse, login_key: str) -> None:
    users = [login_key, *(u for u in known_device_users(request) if u != login_key)][:KNOWN_DEVICE_MAX_USERS]
    response.set_cookie(KNOWN_DEVICE_COOKIE, _device_serializer(request).dumps(users), max_age=KNOWN_DEVICE_S,
                        httponly=True, samesite="lax", secure=request.app.state.https_only)


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html", user=None)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          s: Session = Depends(db, scope="function")):
    # Behind Caddy/nginx the client address comes from X-Forwarded-For (see Settings.trusted_proxies).
    ip = request.client.host if request.client else "?"
    login_key = username.strip().lower()[:MAX_USERNAME_KEY]
    refused = throttle.blocked(ip, login_key, known_device=login_key in known_device_users(request))
    if refused:
        flash(request, refused, "error")
        return RedirectResponse("/login", status_code=303)
    user = s.scalar(select(User).where(User.username == username.strip()))
    # Hash even for unknown users: response time must not reveal which usernames exist.
    stored = user.password_hash if user is not None else _dummy_hash()
    if not verify_password(password, stored) or user is None or not user.is_active:
        throttle.fail(ip, login_key)
        flash(request, "Неверный логин или пароль", "error")
        return RedirectResponse("/login", status_code=303)
    throttle.success(ip, login_key)
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["pw"] = session_stamp(user.password_hash)
    response = RedirectResponse("/", status_code=303)
    _remember_device(request, response, login_key)
    return response


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
