"""Long-polling loop + periodic digests. Runs inside the web process or via `worksaas bot`.

Only ONE process may poll a given bot token (Telegram returns 409 Conflict otherwise).
"""

from __future__ import annotations

import asyncio
import logging

from app.bot.api import TelegramAPI, TelegramError
from app.bot.digest import send_digests
from app.bot.handlers import handle_update, menu_commands
from app.core.config import get_settings

log = logging.getLogger(__name__)


class BotRunner:
    def __init__(self, api: TelegramAPI) -> None:
        self.api = api
        self._stop = asyncio.Event()

    async def run(self) -> None:
        try:
            me = await self.api.get_me()
            await self.api.set_commands([c for c in menu_commands() if c[0] != "start"])
            log.info("telegram bot @%s started", me.get("username"))
        except TelegramError as exc:
            log.error("telegram bot disabled: %s", exc)
            return
        await asyncio.gather(self._poll(), self._digests())

    async def _poll(self) -> None:
        offset: int | None = None
        backoff = 1.0
        while not self._stop.is_set():
            try:
                updates = await self.api.get_updates(offset, timeout=30)
                backoff = 1.0
            except TelegramError as exc:
                log.warning("getUpdates failed: %s", exc)
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await handle_update(self.api, update)
                except Exception:  # noqa: BLE001 - one bad update must not stop the bot
                    log.exception("failed to handle update %s", update.get("update_id"))

    async def _digests(self) -> None:
        interval = get_settings().telegram.digest_interval_s
        while not self._stop.is_set():
            await self._sleep(interval)
            try:
                await send_digests(self.api)
            except Exception:  # noqa: BLE001
                log.exception("digest tick failed")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        self._stop.set()
