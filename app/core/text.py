"""Small text helpers shared by sources, ranking and prompts."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAGS_RE = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|section)[^>]*>", re.I)
_WS_RE = re.compile(r"[ \t ]+")
_NL_RE = re.compile(r"\n{3,}")


def html_to_text(value: str | None) -> str:
    """Crude but dependency-free HTML -> text, keeps paragraph/list breaks."""
    if not value:
        return ""
    text = re.sub(r"<li[^>]*>", "\n• ", value, flags=re.I)
    text = _BLOCK_TAGS_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL_RE.sub("\n\n", text).strip()


_MONTHS_RU = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь",
              "октябрь", "ноябрь", "декабрь")


def current_month(tz_name: str, now: datetime | None = None) -> str:
    """'октябрь 2026' in the app's time zone. Month, not day: prompts that carry it (and so the
    LLM cache keys) change once a month, not every night."""
    now = now or datetime.now(timezone.utc)
    try:
        local = now.astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        local = now
    return f"{_MONTHS_RU[local.month - 1]} {local.year}"


def stable_hash(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[обрезано]"
