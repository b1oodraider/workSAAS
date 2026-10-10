"""FastAPI application factory: wires routers, session middleware, worker and scheduler."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.plugins import load_all
from app.bot.api import get_api, silence_http_logs
from app.bot.runner import BotRunner
from app.core.config import get_settings
from app.core.db import get_engine
from app.jobs.queue import Worker
from app.jobs.scheduler import Scheduler
from app.services.errors import NotFound
from app.web.deps import LoginRequired
from app.web.routes import (
    admin,
    analyses,
    auth,
    autoapply,
    home,
    jobs,
    resumes,
    searches,
    tracker,
    usage,
    vacancies,
)
from app.web.routes import settings as settings_routes
from app.web.templating import WEB_DIR, render

log = logging.getLogger(__name__)

# How long app shutdown waits for running jobs before cancelling them.
SHUTDOWN_TIMEOUT_S = 20


class BodySizeLimit:
    """Reject oversized request bodies before routing: FastAPI parses (and spools to disk)
    multipart uploads before auth dependencies run, so anyone could fill the disk."""

    def __init__(self, app, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_bytes):
            return await self._reject(send)
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge
            return message

        await self.app(scope, limited_receive, send)

    @staticmethod
    async def _reject(send) -> None:
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body", "body": "Слишком большой запрос".encode()})


class SameOriginPosts:
    """CSRF defence on top of SameSite=Lax cookies: refuse state-changing requests sent by
    another site. Browsers mark them with ``Sec-Fetch-Site: cross-site`` and/or an ``Origin``
    that is not ours; requests with neither header (curl, old browsers) pass."""

    UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def __init__(self, app, public_url: str = "") -> None:
        self.app = app
        public = urlsplit(public_url.strip()) if public_url.strip() else None
        self.public_host = _host_port(public.scheme, public.netloc) if public and public.netloc else None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in self.UNSAFE and not self._allowed(scope):
            # Usually a proxy that rewrites Host or a wrong [telegram] public_url, not an attack.
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
            log.warning("refused cross-site %s %s: Origin=%r Sec-Fetch-Site=%r Host=%r X-Forwarded-Host=%r",
                        scope["method"], scope.get("path", ""), headers.get("origin"),
                        headers.get("sec-fetch-site"), headers.get("host"), headers.get("x-forwarded-host"))
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body",
                        "body": "Запрос отклонён: он отправлен с другого сайта".encode()})
            return
        await self.app(scope, receive, send)

    def _allowed(self, scope) -> bool:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
            return False
        origin = headers.get("origin")
        if origin is None:
            return True
        parts = urlsplit(origin.strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False  # "null" (sandboxed frames, privacy redirects) and garbage
        own = {self.public_host}
        scheme = scope.get("scheme", "http")
        for name in ("host", "x-forwarded-host"):  # a cross-site page cannot set these
            value = headers.get(name, "").split(",")[0].strip()
            if value:
                own.add(_host_port(scheme, value))
                own.add(_host_port(parts.scheme, value))  # TLS ended at the proxy
        return _host_port(parts.scheme, parts.netloc) in own


def _host_port(scheme: str, netloc: str) -> str:
    """Lower-case host[:port] without the scheme's default port."""
    netloc = netloc.strip().lower()
    default = {"http": ":80", "https": ":443"}.get(scheme.lower())
    if default and netloc.endswith(default):
        netloc = netloc[: -len(default)]
    return netloc


class _TooLarge(HTTPException):
    # An HTTPException: FastAPI's form parser re-raises those instead of turning them into 400.
    def __init__(self) -> None:
        super().__init__(413, "Слишком большой запрос")


def create_app(*, start_background: bool | None = None) -> FastAPI:
    load_all()
    silence_http_logs()
    settings = get_settings()
    secret_key = settings.secret_key
    if secret_key in ("", "change-me"):
        # Never sign sessions with a publicly known key; users re-login after restarts.
        secret_key = secrets.token_urlsafe(32)
        log.warning("WS_SECRET_KEY is not set: using a random key, sessions reset on restart")
    run_bg = settings.jobs.run_in_web_process if start_background is None else start_background

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        get_engine()
        tasks: list[asyncio.Task] = []
        stoppables: list = []
        if run_bg:
            worker, scheduler = Worker(), Scheduler()
            stoppables += [worker, scheduler]
            tasks += [asyncio.create_task(worker.run()), asyncio.create_task(scheduler.run())]
            api = get_api()
            if api is not None:
                bot = BotRunner(api)
                stoppables.append(bot)
                tasks.append(asyncio.create_task(bot.run()))
        yield
        for item in stoppables:
            item.stop()
        # The bot may be inside a 30 s long-poll request: don't wait for it.
        for task in tasks[2:]:
            task.cancel()
        if tasks:
            # Let running jobs finish, but not forever (process managers kill a slow shutdown);
            # a cancelled job stays 'running' and is requeued on the next start.
            _done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT_S)
            if pending:
                log.warning("shutdown: running jobs did not finish in %s s, cancelling them", SHUTDOWN_TIMEOUT_S)
                for task in pending:
                    task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        api = get_api()
        if api is not None:
            await api.close()

    app = FastAPI(title="workSAAS", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    https_only = settings.session_https_only or settings.telegram.is_https
    # The login route signs its "known device" cookie with the same key and flags.
    app.state.secret_key, app.state.https_only = secret_key, https_only
    app.add_middleware(SessionMiddleware, secret_key=secret_key, same_site="lax",
                       max_age=60 * 60 * 24 * 30, https_only=https_only)
    app.add_middleware(BodySizeLimit, max_bytes=(settings.max_upload_mb + 1) * 1024 * 1024)
    # Outermost: refuse cross-site form posts before reading their bodies.
    app.add_middleware(SameOriginPosts, public_url=settings.telegram.public_url)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    for module in (auth, home, resumes, vacancies, analyses, searches, tracker, autoapply, jobs, usage,
                   settings_routes, admin):
        app.include_router(module.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(_request: Request, _exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, _exc: NotFound):
        response = render(request, "error.html", message="Не найдено")
        response.status_code = 404
        return response

    return app
