"""Jinja2 environment shared by all pages. Feature result views live in app/features/<name>/view.html."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

from app.core.db import to_local
from app.features import FEATURES

WEB_DIR = Path(__file__).parent
FEATURES_DIR = WEB_DIR.parent / "features"

templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.loader = ChoiceLoader(
    [FileSystemLoader(str(WEB_DIR / "templates")), FileSystemLoader(str(FEATURES_DIR))]
)


def score_class(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "s-none"
    # 0..10 scales are shown as-is elsewhere; here scores are 0..100.
    if v >= 75:
        return "s-high"
    if v >= 50:
        return "s-mid"
    return "s-low"


def money(v: Any) -> str:
    salary_from, salary_to, currency = v.salary_from, v.salary_to, v.currency or ""
    if not salary_from and not salary_to:
        return "з/п не указана"
    fmt = lambda x: f"{x:,}".replace(",", " ")  # noqa: E731
    if salary_from and salary_to:
        text = f"{fmt(salary_from)} – {fmt(salary_to)}"
    elif salary_from:
        text = f"от {fmt(salary_from)}"
    else:
        text = f"до {fmt(salary_to)}"
    return f"{text} {currency.replace('RUR', '₽')}".strip()


def dt(value: datetime | None) -> str:
    local = to_local(value)
    return local.strftime("%d.%m.%Y %H:%M") if local else "—"


def day(value: datetime | None) -> str:
    local = to_local(value)
    return local.strftime("%d.%m") if local else ""


def dt_input(value: datetime | None) -> str:
    """Value for <input type=datetime-local> in the configured time zone."""
    local = to_local(value)
    return local.strftime("%Y-%m-%dT%H:%M") if local else ""


def source_title(name: str) -> str:
    from app.sources.registry import SOURCE_CLASSES

    cls = SOURCE_CLASSES.get(name)
    return SOURCE_LABELS.get(name) or (cls.title if cls else name)


def safe_url(value: str | None) -> str:
    """Vacancy URLs come from third-party feeds: allow only http(s) links."""
    return value if value and value.lower().startswith(("http://", "https://")) else ""


SEVERITY_LABELS = {
    "critical": "критично", "major": "важно", "minor": "мелочь",
    "high": "серьёзно", "medium": "заметно", "low": "мелочь",
    "must": "обязательно", "nice": "желательно",
}
JOB_STATUS_LABELS = {"queued": "в очереди", "running": "выполняется", "done": "готово", "failed": "ошибка"}
SOURCE_LABELS = {"manual": "вручную"}

def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


templates.env.globals["FEATURES"] = FEATURES
templates.env.filters.update(score_class=score_class, money=money, dt=dt, day=day, dt_input=dt_input,
                             safe_url=safe_url,
                             severity=lambda v: SEVERITY_LABELS.get(v, v), plural=plural,
                             job_status=lambda v: JOB_STATUS_LABELS.get(v, v),
                             source_title=source_title,
                             tojson_pretty=lambda v: json.dumps(v, ensure_ascii=False, indent=2))


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "text": message})


def pop_flash(request: Request) -> list[dict[str, str]]:
    return request.session.pop("flash", [])


def render(request: Request, name: str, **context: Any):
    context.setdefault("user", getattr(request.state, "user", None))
    context["flashes"] = pop_flash(request)
    return templates.TemplateResponse(request, name, context)
