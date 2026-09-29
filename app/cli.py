"""Command line: DB migrations, user management, running the server/worker.

    worksaas init-db
    worksaas create-user alice [--admin] [--budget 5]
    worksaas set-password alice
    worksaas set-budget alice 10
    worksaas run [--host 127.0.0.1] [--port 8000]
    worksaas worker            # separate worker process (if jobs.run_in_web_process=false)
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


def cmd_run(args) -> None:
    import uvicorn

    _migrate()
    uvicorn.run("app.main:create_app", factory=True, host=args.host, port=args.port,
                log_level="info")


def cmd_worker(_args) -> None:
    import app.services  # noqa: F401
    from app.jobs.queue import Worker
    from app.jobs.scheduler import Scheduler

    _migrate()

    async def main() -> None:
        await asyncio.gather(Worker().run(), Scheduler().run())

    asyncio.run(main())


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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

    p = sub.add_parser("run")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_run)

    sub.add_parser("worker").set_defaults(fn=cmd_worker)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
