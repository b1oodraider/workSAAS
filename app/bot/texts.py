"""Message formatting (HTML parse mode). Every dynamic value goes through esc()."""

from __future__ import annotations

from typing import Any

from app.bot.api import button, clip, esc, keyboard, url_button
from app.core.config import get_settings
from app.models import Vacancy

VERDICTS = {"strong": "сильное совпадение", "good": "хорошее", "stretch": "с натяжкой",
            "weak": "слабое", "mismatch": "не подходит"}
RECOMMEND = {"apply": "откликаться", "apply_with_caveats": "откликаться с оговорками", "skip": "пропустить"}


def web_url(path: str) -> str:
    return get_settings().telegram.public_url.rstrip("/") + path


def autoapply_pause_keyboard(can_resume: bool) -> dict[str, Any]:
    rows = [[button("▶️ Продолжить", "ar:1")]] if can_resume else []
    rows.append([url_button("Открыть автоотклики", web_url("/autoapply"))])
    return keyboard(*rows)


def salary(v: Vacancy) -> str:
    if not v.salary_from and not v.salary_to:
        return "з/п не указана"
    cur = (v.currency or "").replace("RUR", "₽")
    fmt = lambda x: f"{x:,}".replace(",", " ")  # noqa: E731
    if v.salary_from and v.salary_to:
        return f"{fmt(v.salary_from)}–{fmt(v.salary_to)} {cur}".strip()
    return (f"от {fmt(v.salary_from)}" if v.salary_from else f"до {fmt(v.salary_to)}") + f" {cur}".rstrip()


def vacancy_header(v: Vacancy) -> str:
    parts = [f"<b>{esc(clip(v.title, 200))}</b>"]
    meta = " · ".join(esc(clip(x, 80)) for x in (v.company, v.location, salary(v)) if x)
    if meta:
        parts.append(meta)
    return "\n".join(parts)


def match_block(m: dict[str, Any]) -> str:
    lines = [f"🎯 <b>{esc(m.get('score'))}/100</b> — {esc(VERDICTS.get(m.get('verdict'), m.get('verdict')))}, "
             f"{esc(RECOMMEND.get(m.get('recommendation'), ''))}",
             esc(clip(m.get("summary", ""), 600))]
    gaps = [str(g.get("requirement") or "") for g in m.get("gaps") or []
            if isinstance(g, dict) and g.get("importance") == "must"]
    if gaps:
        lines.append("Пробелы: " + esc(clip(", ".join(gaps[:4]), 300)))
    return "\n".join(lines)


def review_block(r: dict[str, Any]) -> str:
    lines = [f"🔎 Вакансия: <b>{esc(r.get('overall_score'))}/100</b>. {esc(clip(r.get('summary', ''), 500))}"]
    flags = [f for f in r.get("red_flags") or [] if isinstance(f, dict) and f.get("severity") in ("high", "medium")]
    for f in flags[:3]:
        lines.append(f"🚩 {esc(clip(f.get('text'), 200))}")
    return "\n".join(lines)


def vacancy_card(v: Vacancy, match: dict | None, review: dict | None) -> str:
    parts = [vacancy_header(v)]
    if match:
        parts.append(match_block(match))
    if review:
        parts.append(review_block(review))
    links = [f'<a href="{esc(web_url(f"/vacancies/{v.id}"))}">подробнее</a>']
    if v.url and v.url.startswith(("http://", "https://")):
        links.append(f'<a href="{esc(v.url)}">на сайте</a>')
    parts.append(" · ".join(links))
    return "\n\n".join(parts)
