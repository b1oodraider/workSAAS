"""Per-user saved browser sessions (Playwright storage_state) for job sites.

A session file gives access to the user's job-site account: it is stored with 0600
permissions, filtered to the site's own cookies, and never shown back in the UI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.errors import ValidationFailed

SITE_DOMAINS = {"hh": ("hh.ru", "hh.kz", "hh.uz")}
MAX_SESSION_BYTES = 512 * 1024


def session_path(site: str, user_id: int) -> Path:
    return Path(get_settings().data_dir) / "sessions" / f"{site}_{int(user_id)}.json"


def has_session(site: str, user_id: int) -> bool:
    return session_path(site, user_id).is_file()


def _belongs(domain: str, site: str) -> bool:
    domain = domain.lstrip(".").lower()
    return any(domain == d or domain.endswith("." + d) for d in SITE_DOMAINS[site])


def clean_state(site: str, raw: Any) -> dict[str, Any]:
    """Validate a storage_state and keep only this site's cookies and origins."""
    if not isinstance(raw, dict) or not isinstance(raw.get("cookies"), list):
        raise ValidationFailed("Это не файл сессии браузера (ожидается storage_state из worksaas hh-login)")
    cookies = [c for c in raw["cookies"] if isinstance(c, dict) and _belongs(str(c.get("domain", "")), site)]
    if not cookies:
        raise ValidationFailed("В файле нет cookies для hh.ru — войдите в hh.ru и экспортируйте сессию заново")
    origins = [o for o in raw.get("origins") or []
               if isinstance(o, dict) and _belongs(str(o.get("origin", "")).split("//")[-1].split("/")[0], site)]
    return {"cookies": cookies, "origins": origins}


def save_session(site: str, user_id: int, raw: Any) -> None:
    state = clean_state(site, raw)
    path = session_path(site, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def save_session_bytes(site: str, user_id: int, data: bytes) -> None:
    if len(data) > MAX_SESSION_BYTES:
        raise ValidationFailed("Файл сессии слишком большой")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Файл сессии повреждён (не JSON)") from exc
    save_session(site, user_id, raw)


def load_session(site: str, user_id: int) -> dict[str, Any] | None:
    path = session_path(site, user_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def delete_session(site: str, user_id: int) -> None:
    session_path(site, user_id).unlink(missing_ok=True)
