"""Per-user saved browser sessions (Playwright storage_state) for job sites.

A session file gives access to the user's job-site account: it is stored with 0600
permissions, filtered to the site's own cookies, and never shown back in the UI.
Appliers use these helpers through `Applier.save_session` & co, which supply the site's domains.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.core.config import get_settings
from app.core.errors import ValidationFailed

MAX_SESSION_BYTES = 512 * 1024


def session_path(site: str, user_id: int) -> Path:
    return Path(get_settings().data_dir) / "sessions" / f"{site}_{int(user_id)}.json"


def _belongs(domain: str, domains: tuple[str, ...]) -> bool:
    domain = domain.lstrip(".").lower()
    return any(domain == d or domain.endswith("." + d) for d in domains)


def clean_state(raw: Any, domains: tuple[str, ...], title: str) -> dict[str, Any]:
    """Validate a storage_state and keep only the site's cookies and origins."""
    if not isinstance(raw, dict) or not isinstance(raw.get("cookies"), list):
        raise ValidationFailed("Это не тот файл: нужен файл, который создаёт команда worksaas site-login … --export")
    cookies = [c for c in raw["cookies"] if isinstance(c, dict) and _belongs(str(c.get("domain", "")), domains)]
    if not cookies:
        raise ValidationFailed(f"В файле нет cookies для {title} — войдите на сайт и экспортируйте сессию заново")
    origins = [o for o in raw.get("origins") or []
               if isinstance(o, dict) and _belongs(urlsplit(str(o.get("origin", ""))).hostname or "", domains)]
    return {"cookies": cookies, "origins": origins}


def parse_bytes(data: bytes) -> Any:
    if len(data) > MAX_SESSION_BYTES:
        raise ValidationFailed("Файл сессии слишком большой")
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:  # bad UTF-8, not JSON, absurdly nested
        raise ValidationFailed("Файл повреждён — создайте его заново командой worksaas site-login") from exc


def write_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic write readable only by the app's user (a unique temp file: no races, no symlinks)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".session-", suffix=".tmp")  # created 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
