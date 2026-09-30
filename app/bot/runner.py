"""Long-polling loop + periodic digests. Runs inside the web process or via `worksaas bot`.

Only ONE process may poll a given bot token (Telegram returns 409 Conflict otherwise).
"""

from __future__ import annotations

import asyncio
import logging

from app.bot.api import TelegramAPI, TelegramError
from app.bot.digest import send_autoapply_updates, send_digests, send_reminders
from app.bot.handlers import handle_update, menu_commands
from app.core.config import get_settings

log = logging.getLogger(__name__)


class BotRunner:
    def __init__(self, api: TelegramAPI) -> None:
        self.api = api
        self._stop = asyncio.Event()
        self._conflict = False

    async def run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                me = await self.api.get_me()
                await self.api.set_commands([c for c in menu_commands() if c[0] != "start"])
                log.info("telegram bot @%s started", me.get("username"))
                break
            except TelegramError as exc:
                if exc.code == 401:
                    log.error("telegram bot disabled: invalid token")
                    return
                log.warning("telegram not reachable yet (%s), retry in %.0fs", exc, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, 300)
        self._conflict = False
        await asyncio.gather(self._poll(), self._digests())

    async def _poll(self) -> None:
        offset: int | None = None
        backoff = 1.0
        while not self._stop.is_set():
            try:
                updates = await self.api.get_updates(offset, timeout=30)
                backoff = 1.0
                self._conflict = False
            except TelegramError as exc:
                # 409: another process polls this token — pause pushes too, or users get duplicates.
                self._conflict = exc.conflict
                log.warning("getUpdates failed: %s", exc)
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            except Exception:  # noqa: BLE001 - never let the poll loop die
                log.exception("getUpdates crashed")
                await self._sleep(backoff)
                continue
            for update in updates if isinstance(updates, list) else []:
                try:
                    offset = int(update["update_id"]) + 1
                    await handle_update(self.api, update)
                except Exception:  # noqa: BLE001 - one bad update must not stop the bot
                    log.exception("failed to handle an update")
            if updates:
                # Confirm the batch right away so a restart doesn't replay handled updates.
                try:
                    await self.api.get_updates(offset, timeout=0)
                except TelegramError:
                    pass

    async def _digests(self) -> None:
        interval = get_settings().telegram.digest_interval_s
        while not self._stop.is_set():
            await self._sleep(interval)
            if self._conflict:
                continue
            for tick in (send_digests, send_reminders, send_autoapply_updates):
                try:
                    await tick(self.api)
                except Exception:  # noqa: BLE001
                    log.exception("%s failed", tick.__name__)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        self._stop.set()
