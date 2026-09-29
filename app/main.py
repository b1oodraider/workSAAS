"""FastAPI application factory: wires routers, session middleware, worker and scheduler."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.plugins import load_all
from app.bot.api import get_api
from app.bot.runner import BotRunner
from app.core.config import get_settings
from app.core.db import get_engine
from app.jobs.queue import Worker
from app.jobs.scheduler import Scheduler
from app.services.errors import NotFound
from app.web.deps import LoginRequired
from app.web.routes import analyses, auth, jobs, resumes, searches, settings as settings_routes, usage, vacancies
from app.web.templating import WEB_DIR, render

log = logging.getLogger(__name__)


def create_app(*, start_background: bool | None = None) -> FastAPI:
    load_all()
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
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="workSAAS", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret_key, same_site="lax",
                       max_age=60 * 60 * 24 * 30)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    for module in (auth, resumes, vacancies, analyses, searches, jobs, usage, settings_routes):
        app.include_router(module.router)

    @app.get("/")
    def index():
        return RedirectResponse("/resumes", status_code=303)

    @app.exception_handler(LoginRequired)
    async def _login_required(_request: Request, _exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, _exc: NotFound):
        response = render(request, "error.html", message="Не найдено")
        response.status_code = 404
        return response

    return app
