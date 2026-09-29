"""Minimal async Telegram Bot API client (httpx, long polling). No extra dependencies."""

from __future__ import annotations

import html
import json
import logging
from typing import Any

import httpx

from app.core.config import get_settings

log = logging.getLogger(__name__)

MAX_TEXT = 4096


class TelegramError(Exception):
    pass


def esc(value: Any) -> str:
    """Escape text for parse_mode=HTML. All third-party/user text must go through this."""
    return html.escape(str(value if value is not None else ""), quote=False)


def button(text: str, data: str) -> dict[str, str]:
    if len(data.encode()) > 64:
        raise ValueError("callback_data is limited to 64 bytes")
    return {"text": text, "callback_data": data}


def url_button(text: str, url: str) -> dict[str, str]:
    return {"text": text, "url": url}


def keyboard(*rows: list[dict[str, str]]) -> dict[str, Any]:
    return {"inline_keyboard": [list(r) for r in rows if r]}


class TelegramAPI:
    def __init__(self, token: str, *, use_proxy: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        proxy = get_settings().proxy_url if use_proxy else None
        # Long polling holds the request open; keep read timeout above getUpdates timeout.
        self.client = httpx.AsyncClient(proxy=proxy if transport is None else None,
                                        timeout=httpx.Timeout(10.0, read=60.0),
                                        transport=transport)
        self.username: str | None = None

    async def call(self, method: str, **params: Any) -> Any:
        payload = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                   for k, v in params.items() if v is not None}
        try:
            resp = await self.client.post(f"{self.base}/{method}", data=payload)
        except httpx.HTTPError as exc:
            # Never include the URL: it contains the bot token.
            raise TelegramError(f"{method}: network error {type(exc).__name__}") from None
        try:
            body = resp.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {resp.status_code}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', resp.status_code)}")
        return body.get("result")

    async def get_me(self) -> dict[str, Any]:
        me = await self.call("getMe")
        self.username = me.get("username")
        return me

    async def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        return await self.call("getUpdates", offset=offset, timeout=timeout,
                               allowed_updates=["message", "callback_query"])

    async def send(self, chat_id: int, text: str, *, reply_markup: dict[str, Any] | None = None,
                   disable_preview: bool = True) -> dict[str, Any]:
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 20] + "\n…"
        return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                               reply_markup=reply_markup,
                               link_preview_options={"is_disabled": disable_preview})

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text or None)

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        await self.call("setMyCommands",
                        commands=[{"command": c, "description": d} for c, d in commands])

    async def close(self) -> None:
        await self.client.aclose()


_api: TelegramAPI | None = None


def get_api() -> TelegramAPI | None:
    """Shared client for job handlers and the runner; None when the bot is not configured."""
    global _api
    cfg = get_settings().telegram
    if not cfg.active:
        return None
    if _api is None:
        _api = TelegramAPI(cfg.bot_token or "", use_proxy=cfg.use_proxy)
    return _api


def set_api(api: TelegramAPI | None) -> None:
    global _api
    _api = api
