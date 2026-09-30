"""Command line: DB migrations, user management, running the server/worker.

    worksaas init-db
    worksaas create-user alice [--admin] [--budget 5]
    worksaas set-password alice
    worksaas set-budget alice 10
    worksaas doctor [--llm]    # check config, job sites, bot token, LLM access
    worksaas site-login hh alice   # log in to hh.ru in a browser window, save session for auto-apply
    worksaas backup [path]     # consistent SQLite copy, safe while running
    worksaas run [--host 127.0.0.1] [--port 8000]
    worksaas worker            # separate worker process (if jobs.run_in_web_process=false)
    worksaas bot               # Telegram bot only (if it doesn't run inside the web process)
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")


def _ask_password() -> str:
    while True:
        pw = getpass.getpass("Пароль: ")
        if len(pw) < 8:
            print("Минимум 8 символов")
            continue
        if pw == getpass.getpass("Повторите: "):
            return pw
        print("Пароли не совпадают")


def cmd_init_db(_args) -> None:
    _migrate()
    print("База данных готова")


def cmd_create_user(args) -> None:
    from app.core.db import session_scope
    from app.core.security import hash_password
    from app.models import User

    _migrate()
    password = args.password or _ask_password()
    with session_scope() as s:
        if s.scalar(select(User).where(User.username == args.username)):
            sys.exit(f"Пользователь {args.username} уже существует")
        s.add(User(username=args.username, password_hash=hash_password(password),
                   is_admin=args.admin, monthly_budget_usd=args.budget))
    print(f"Создан пользователь {args.username}")


def _get_user(s, username: str):
    from app.models import User

    user = s.scalar(select(User).where(User.username == username))
    if user is None:
        sys.exit(f"Пользователь {username} не найден")
    return user


def cmd_set_password(args) -> None:
    from app.core.db import session_scope
    from app.core.security import hash_password

    password = args.password or _ask_password()
    with session_scope() as s:
        _get_user(s, args.username).password_hash = hash_password(password)
    print("Пароль обновлён")


def cmd_set_budget(args) -> None:
    from app.core.db import session_scope

    with session_scope() as s:
        _get_user(s, args.username).monthly_budget_usd = args.usd
    print("Бюджет обновлён (0 = без лимита)")


def cmd_backup(args) -> None:
    """Consistent copy of the SQLite database while the app keeps running."""
    import sqlite3
    from datetime import datetime

    from app.core.config import get_settings

    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        sys.exit("backup поддерживает только SQLite; для Postgres используйте pg_dump")
    src_path = Path(url.split("///", 1)[1])
    if not src_path.exists():
        sys.exit(f"База не найдена: {src_path}")
    target = Path(args.path) if args.path else src_path.parent / "backups" / (
        f"worksaas-{datetime.now():%Y%m%d-%H%M%S}.db")
    target.parent.mkdir(parents=True, exist_ok=True)
    src, dst = sqlite3.connect(src_path), sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"Копия базы: {target}")


def cmd_site_login(args) -> None:
    """Open a visible browser, let the user log in to a job site, save the session for auto-apply."""
    from app.apply import applier_for, supported_sources
    from app.apply import sessions
    from app.core.config import get_settings
    from app.core.db import session_scope
    from app.core.errors import ValidationFailed
    from app.core.http import chromium_launch_options

    applier = applier_for(args.site)
    if applier is None:
        sys.exit(f"Автоотклики для «{args.site}» не поддерживаются. Доступно: {', '.join(supported_sources())}")
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit('Нужен браузерный режим: pip install -e ".[browser]" && playwright install chromium')

    user_id = None
    if not args.export and not args.username:
        sys.exit(f"Укажите свой логин в workSAAS (worksaas site-login {args.site} alice) или --export файл.json")
    if not args.export:
        _migrate()
        with session_scope() as s:
            user_id = _get_user(s, args.username).id

    def ask(prompt: str) -> None:
        print(prompt)
        input()

    async def login() -> dict:
        cfg = get_settings().apply
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**chromium_launch_options(
                headless=False, use_proxy=cfg.use_proxy, executable=cfg.browser_executable))
            try:
                context = await browser.new_context(locale="ru-RU")
                page = await context.new_page()
                await page.goto(applier.login_url)
                prompt = (f"В открывшемся окне войдите в {applier.title} как обычно (с кодом из SMS/почты).\n"
                          "Когда увидите свой профиль, вернитесь сюда и нажмите Enter.")
                for _ in range(3):
                    await asyncio.get_running_loop().run_in_executor(None, ask, prompt)
                    if not applier.account_url:
                        break
                    # The site sets cookies for anonymous visitors too: make sure we are really logged in.
                    await page.goto(applier.account_url)
                    if "login" not in page.url and "signup" not in page.url:
                        break
                    prompt = "Похоже, вход ещё не выполнен. Войдите в окне браузера и нажмите Enter ещё раз."
                else:
                    raise SystemExit("Вход не выполнен — запустите команду ещё раз.")
                return await context.storage_state()
            finally:
                await browser.close()

    try:
        state = asyncio.run(login())
    except PlaywrightError:
        sys.exit("Окно браузера закрылось раньше времени — запустите команду ещё раз.")
    try:
        cleaned = applier.clean_session(state)
    except ValidationFailed as exc:
        sys.exit(f"Не получилось: {exc}")
    page_url = get_settings().telegram.public_url.rstrip("/") + "/autoapply"
    if args.export:
        path = Path(args.export)
        sessions.write_state(path.resolve(), cleaned)
        print(f"Готово: {path}. Загрузите файл на странице «Автоотклики» ({page_url}) и затем удалите его — "
              "он даёт доступ к вашему аккаунту.")
    else:
        applier.save_session(user_id, cleaned)
        print(f"Вход в {applier.title} сохранён. Включите автоотклики или нажмите «Продолжить» на странице "
              f"«Автоотклики»: {page_url}")


def cmd_doctor(args) -> None:
    from app.doctor import run

    checks = asyncio.run(run(llm=args.llm))
    for c in checks:
        print(f"{'✅' if c.ok else '❌'} {c.name}{' — ' + c.detail if c.detail else ''}")
    failed = [c for c in checks if not c.ok]
    if not args.llm:
        print("\nПроверить реальный запрос к модели (стоит доли цента): worksaas doctor --llm")
    sys.exit(1 if failed else 0)


def cmd_run(args) -> None:
    import uvicorn

    _migrate()
    uvicorn.run("app.main:create_app", factory=True, host=args.host, port=args.port,
                log_level="info")


def cmd_worker(_args) -> None:
    from app.jobs.queue import Worker
    from app.plugins import load_all
    from app.jobs.scheduler import Scheduler

    load_all()
    _migrate()

    async def main() -> None:
        await asyncio.gather(Worker().run(), Scheduler().run())

    asyncio.run(main())


def cmd_bot(_args) -> None:
    from app.bot.api import get_api
    from app.bot.runner import BotRunner
    from app.plugins import load_all

    load_all()
    _migrate()
    api = get_api()
    if api is None:
        sys.exit("Бот не настроен: задайте WS_TELEGRAM__BOT_TOKEN в .env")
    asyncio.run(BotRunner(api).run())


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from app.bot.api import silence_http_logs

    silence_http_logs()
    parser = argparse.ArgumentParser(prog="worksaas")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)

    p = sub.add_parser("create-user")
    p.add_argument("username")
    p.add_argument("--admin", action="store_true")
    p.add_argument("--budget", type=float, default=None, help="USD в месяц; по умолчанию из конфига")
    p.add_argument("--password", help="не рекомендуется: останется в истории shell")
    p.set_defaults(fn=cmd_create_user)

    p = sub.add_parser("set-password")
    p.add_argument("username")
    p.add_argument("--password")
    p.set_defaults(fn=cmd_set_password)

    p = sub.add_parser("set-budget")
    p.add_argument("username")
    p.add_argument("usd", type=float)
    p.set_defaults(fn=cmd_set_budget)

    p = sub.add_parser("site-login", help="войти на сайт вакансий (hh) в окне браузера — для автооткликов")
    p.add_argument("site", help="сайт: hh")
    p.add_argument("username", nargs="?", help="ваш логин в workSAAS (не нужен с --export)")
    p.add_argument("--export", help="сохранить вход в файл, чтобы загрузить его на сервер через веб")
    p.set_defaults(fn=cmd_site_login)

    p = sub.add_parser("hh-login", help="то же, что site-login hh")
    p.add_argument("username", nargs="?", help="ваш логин в workSAAS (не нужен с --export)")
    p.add_argument("--export", help="сохранить вход в файл, чтобы загрузить его на сервер через веб")
    p.set_defaults(fn=cmd_site_login, site="hh")

    p = sub.add_parser("doctor", help="проверить настройки, доступность сайтов, бота и LLM")
    p.add_argument("--llm", action="store_true", help="сделать пробный запрос к каждой модели из маршрутов")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("backup", help="копия базы SQLite (можно на работающем приложении)")
    p.add_argument("path", nargs="?", help="куда сохранить; по умолчанию data/backups/")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("run")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_run)

    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    sub.add_parser("bot", help="только Telegram-бот (если веб запущен с telegram.enabled=false)").set_defaults(fn=cmd_bot)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
