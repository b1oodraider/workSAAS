"""Auto-apply: pick the best matches, write a letter, send ONE application at a time.

Guard rails (never exceeded, whatever the user sets):
- at most `apply.hard_daily_max` applications per user per day, and the user's own limit;
- at least `apply.min_interval_floor_s` between applications (plus jitter);
- only within the user's active hours (local time);
- only vacancies with a match score >= min_score and recommendation != "skip",
  without serious red flags in the vacancy review;
- anything that looks like a captcha/logout ("blocked"), a spent budget or several
  failures in a row pauses auto-apply until the user resumes it.
In "confirm" mode nothing is sent before the user approves it (web page or Telegram).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.apply import ApplyRequest, applier_for, supported_sources
from app.core.config import get_settings
from app.core.db import session_scope, to_local, utcnow
from app.core.errors import NotFound, UserError, ValidationFailed
from app.jobs import JobContext, enqueue, job_handler
from app.jobs.scheduler import periodic
from app.llm import BudgetExceeded
from app.models import (
    ACTIVE_APPLICATION_STATUSES,
    Analysis,
    Application,
    ApplicationStatus,
    AutoApplySettings,
    User,
    Vacancy,
)
from app.services import analysis as analysis_svc
from app.services import matches as matches_svc
from app.services import resumes as resume_svc
from app.services import vacancies as vacancy_svc

log = logging.getLogger(__name__)

MODES = ("confirm", "auto")
TONES = ("friendly", "formal", "concise", "enthusiastic")


# --------------------------------------------------------------------------- settings


def get_config(s: Session, user_id: int) -> AutoApplySettings:
    cfg = s.get(AutoApplySettings, user_id)
    if cfg is None:
        cfg = AutoApplySettings(user_id=user_id)
        s.add(cfg)
        s.flush()
    return cfg


def update_config(s: Session, user_id: int, *, enabled: bool, mode: str, min_score: int, daily_limit: int,
                  min_interval_s: int, active_from_hour: int, active_to_hour: int,
                  resume_id: int | None, hh_resume_title: str, letter_tone: str) -> AutoApplySettings:
    guard = get_settings().apply
    if mode not in MODES:
        raise ValidationFailed("Неизвестный режим")
    if letter_tone not in TONES:
        raise ValidationFailed("Неизвестный тон письма")
    if not (0 <= active_from_hour < active_to_hour <= 24):
        raise ValidationFailed("Часы работы: «с» должно быть меньше «до», в пределах 0–24")
    if resume_id is not None:
        resume_svc.get_owned(s, user_id, resume_id)
    cfg = get_config(s, user_id)
    cfg.enabled = enabled
    cfg.mode = mode
    cfg.min_score = max(50, min(int(min_score), 100))
    cfg.daily_limit = max(1, min(int(daily_limit), guard.hard_daily_max))
    cfg.min_interval_s = max(guard.min_interval_floor_s, int(min_interval_s))
    cfg.active_from_hour, cfg.active_to_hour = active_from_hour, active_to_hour
    cfg.resume_id = resume_id
    cfg.hh_resume_title = hh_resume_title.strip()[:200]
    cfg.letter_tone = letter_tone
    if enabled:
        cfg.paused_reason, cfg.paused_notified = "", False
    return cfg


def pause(s: Session, user_id: int, reason: str) -> None:
    cfg = get_config(s, user_id)
    cfg.paused_reason = reason[:500] or "пауза"
    cfg.paused_notified = False


def resume_after_pause(s: Session, user_id: int) -> None:
    cfg = get_config(s, user_id)
    cfg.paused_reason, cfg.paused_notified = "", False


def is_running(cfg: AutoApplySettings) -> bool:
    return bool(cfg.enabled and not cfg.paused_reason and get_settings().apply.enabled)


# --------------------------------------------------------------------------- selection


def _day_start_utc(now: datetime) -> datetime:
    """Midnight of the user's local day, in UTC (daily limits follow the user's calendar)."""
    from app.core.db import from_local

    local = to_local(now)
    return from_local(local.replace(hour=0, minute=0, second=0, microsecond=0))


def sent_today(s: Session, user_id: int, now: datetime | None = None) -> int:
    start = _day_start_utc(now or utcnow())
    return s.scalar(select(func.count(Application.id)).where(
        Application.user_id == user_id, Application.sent_at >= start,
        Application.status.in_([ApplicationStatus.applied, ApplicationStatus.sending]))) or 0


def _has_serious_red_flags(s: Session, user_id: int, vacancy_id: int) -> bool:
    review = analysis_svc.latest(s, user_id, "vacancy_review", vacancy_id=vacancy_id)
    flags = (review.output or {}).get("red_flags") if review else None
    return any(isinstance(f, dict) and f.get("severity") == "high" for f in flags or [])


def candidates(s: Session, user_id: int, cfg: AutoApplySettings) -> list[tuple[Vacancy, Analysis]]:
    """Best open matches that could be auto-applied, best first."""
    taken = set(s.scalars(select(Application.vacancy_id).where(Application.user_id == user_id)))
    sources = set(supported_sources())
    result = []
    for _uv, v, a in matches_svc.top_matches(s, user_id, min_score=cfg.min_score):
        if v.id in taken or v.source not in sources:
            continue
        if cfg.resume_id and a.resume_id != cfg.resume_id:
            continue
        if (a.output or {}).get("recommendation") == "skip":
            continue
        if _has_serious_red_flags(s, user_id, v.id):
            continue
        result.append((v, a))
    return result


def plan(s: Session, user_id: int) -> list[int]:
    """Top up the queue so that queued + sent today never exceeds the daily limit."""
    cfg = get_config(s, user_id)
    active = s.scalar(select(func.count(Application.id)).where(
        Application.user_id == user_id,
        Application.status.in_([ApplicationStatus.queued, ApplicationStatus.approved]))) or 0
    room = cfg.daily_limit - sent_today(s, user_id) - active
    added = []
    for v, a in candidates(s, user_id, cfg)[:max(room, 0)]:
        app = Application(user_id=user_id, vacancy_id=v.id, resume_id=a.resume_id, score=a.score)
        s.add(app)
        s.flush()
        added.append(app.id)
    return added


# --------------------------------------------------------------------------- sending


def _jittered_interval(cfg: AutoApplySettings, app_id: int) -> timedelta:
    # Deterministic 0-50% jitter so applications don't go out like clockwork.
    return timedelta(seconds=cfg.min_interval_s * (1 + (app_id * 37 % 50) / 100))


def next_to_send(s: Session, user_id: int, now: datetime | None = None) -> Application | None:
    """The application that may be sent right now, or None (limits, hours, nothing ready)."""
    now = now or utcnow()
    cfg = get_config(s, user_id)
    if not is_running(cfg):
        return None
    if not (cfg.active_from_hour <= to_local(now).hour < cfg.active_to_hour):
        return None
    if sent_today(s, user_id, now) >= min(cfg.daily_limit, get_settings().apply.hard_daily_max):
        return None
    if s.scalar(select(func.count(Application.id)).where(
            Application.user_id == user_id, Application.status == ApplicationStatus.sending)):
        return None
    ready_status = ApplicationStatus.approved if cfg.mode == "confirm" else ApplicationStatus.queued
    app = s.scalar(select(Application).where(Application.user_id == user_id, Application.status == ready_status)
                   .order_by(Application.score.desc().nullslast(), Application.id).limit(1))
    if app is None:
        return None
    last = s.scalar(select(func.max(Application.sent_at)).where(Application.user_id == user_id))
    if last and last + _jittered_interval(cfg, app.id) > now:
        return None
    return app


@periodic
def autoapply_tick() -> list[int]:
    """Scheduler tick: plan queues and start at most one sending job per user."""
    started = []
    with session_scope() as s:
        user_ids = list(s.scalars(
            select(AutoApplySettings.user_id).join(User, User.id == AutoApplySettings.user_id)
            .where(AutoApplySettings.enabled.is_(True), AutoApplySettings.paused_reason == "",
                   User.is_active.is_(True))))
    for user_id in user_ids:
        with session_scope() as s:
            if not is_running(get_config(s, user_id)):
                continue
            plan(s, user_id)
            app = next_to_send(s, user_id)
            if app is None:
                continue
            app.status = ApplicationStatus.sending
            app.sent_at = utcnow()  # reserves the slot: the interval counts from here
            app_id = app.id
        enqueue("autoapply_send", {"application_id": app_id}, user_id=user_id, title="Автоотклик")
        started.append(app_id)
    return started




def _finish(app_id: int, status: ApplicationStatus, reason: str, *, sent: bool) -> None:
    with session_scope() as s:
        app = s.get(Application, app_id)
        app.status = status
        app.reason = reason[:1000]
        if not sent:
            app.sent_at = None  # nothing went out: don't count it against limits/interval
        app.notified_at = None


def _consecutive_failures(s: Session, user_id: int) -> int:
    recent = s.scalars(
        select(Application.status).where(
            Application.user_id == user_id,
            Application.status.in_([ApplicationStatus.applied, ApplicationStatus.failed]))
        .order_by(Application.id.desc()).limit(10))
    count = 0
    for status in recent:
        if status != ApplicationStatus.failed:
            break
        count += 1
    return count


@job_handler("autoapply_send")
async def _send_job(ctx: JobContext) -> dict:
    app_id = int(ctx.payload["application_id"])
    with session_scope() as s:
        app = s.get(Application, app_id)
        if app is None or app.user_id != ctx.user_id or app.status != ApplicationStatus.sending:
            return {"skipped": "application is not in sending state"}
        cfg = get_config(s, ctx.user_id)
        vacancy = app.vacancy
        applier = applier_for(vacancy.source)
        req_base = dict(user_id=ctx.user_id, source=vacancy.source, external_id=vacancy.external_id,
                        url=vacancy.url, site_resume_title=cfg.hh_resume_title)
        letter = app.letter
        mode, tone = cfg.mode, cfg.letter_tone
        resume_id, vacancy_id = app.resume_id, app.vacancy_id

    if applier is None:
        _finish(app_id, ApplicationStatus.skipped, "для этого сайта автоотклик не поддерживается", sent=False)
        return {"status": "skipped"}

    if not letter:
        try:
            analysis = await analysis_svc.run_analysis(
                ctx.user_id, "cover_letter", resume_id=resume_id, vacancy_id=vacancy_id,
                params={"tone": tone, "length": "short"})
        except BudgetExceeded as exc:
            _finish(app_id, ApplicationStatus.queued, "ждёт: исчерпан бюджет на ИИ", sent=False)
            with session_scope() as s:
                pause(s, ctx.user_id, str(exc))
            return {"status": "paused", "reason": "budget"}
        except UserError as exc:
            _finish(app_id, ApplicationStatus.failed, f"не удалось написать письмо: {exc}", sent=False)
            return {"status": "failed"}
        letter = (analysis.output or {}).get("body", "")
        with session_scope() as s:
            s.get(Application, app_id).letter = letter

    result = await applier.apply(ApplyRequest(letter=letter, **req_base))
    if result.status == "applied":
        _finish(app_id, ApplicationStatus.applied, result.reason, sent=True)
        with session_scope() as s:
            vacancy_svc.set_status(s, ctx.user_id, vacancy_id, "applied")
    elif result.status == "skipped":
        _finish(app_id, ApplicationStatus.skipped, result.reason, sent=False)
    elif result.status == "blocked":
        # Nothing was sent: put it back in line and stop until the user looks at it.
        back = ApplicationStatus.approved if mode == "confirm" else ApplicationStatus.queued
        _finish(app_id, back, result.reason, sent=False)
        with session_scope() as s:
            pause(s, ctx.user_id, result.reason)
    else:
        _finish(app_id, ApplicationStatus.failed, result.reason, sent=False)
        with session_scope() as s:
            limit = get_settings().apply.max_consecutive_failures
            if _consecutive_failures(s, ctx.user_id) >= limit:
                pause(s, ctx.user_id, f"{limit} ошибки подряд — проверьте сессию hh.ru и настройки")
    return {"status": result.status, "reason": result.reason, "result_url": "/autoapply"}


# --------------------------------------------------------------------------- user actions


def get_owned(s: Session, user_id: int, app_id: int) -> Application:
    app = s.get(Application, app_id)
    if app is None or app.user_id != user_id:
        raise NotFound("application")
    return app


def approve(s: Session, user_id: int, app_id: int) -> None:
    app = get_owned(s, user_id, app_id)
    if app.status == ApplicationStatus.queued:
        app.status = ApplicationStatus.approved


def cancel(s: Session, user_id: int, app_id: int) -> None:
    app = get_owned(s, user_id, app_id)
    if app.status in (ApplicationStatus.queued, ApplicationStatus.approved):
        app.status = ApplicationStatus.cancelled


def overview(s: Session, user_id: int) -> dict[str, Any]:
    cfg = get_config(s, user_id)
    apps = list(s.scalars(select(Application).where(Application.user_id == user_id)
                          .order_by(Application.id.desc()).limit(200)))
    readiness = {}
    for source in supported_sources():
        ok, hint = applier_for(source).is_ready(user_id)
        readiness[source] = {"ok": ok, "hint": hint, "title": applier_for(source).title}
    return {
        "cfg": cfg,
        "running": is_running(cfg),
        "queue": [a for a in apps if a.status in ACTIVE_APPLICATION_STATUSES],
        "history": [a for a in apps if a.status not in ACTIVE_APPLICATION_STATUSES],
        "sent_today": sent_today(s, user_id),
        "hard_daily_max": get_settings().apply.hard_daily_max,
        "min_interval_floor_s": get_settings().apply.min_interval_floor_s,
        "readiness": readiness,
        "resumes": resume_svc.list_for_user(s, user_id),
    }


# --------------------------------------------------------------------------- site sessions


def save_site_session(user_id: int, data: bytes, site: str = "hh") -> None:
    from app.apply import sessions

    sessions.save_session_bytes(site, user_id, data)


def delete_site_session(user_id: int, site: str = "hh") -> None:
    from app.apply import sessions

    sessions.delete_session(site, user_id)


# --------------------------------------------------------------------------- notifications (bot)


def claim_for_notification(ids: list[int]) -> list[int]:
    """Mark applications as reported; returns ids this process won (safe with two bot processes)."""
    from sqlalchemy import update

    won = []
    with session_scope() as s:
        for app_id in ids:
            res = s.execute(update(Application).where(Application.id == app_id, Application.notified_at.is_(None))
                            .values(notified_at=utcnow()))
            if res.rowcount:
                won.append(app_id)
    return won


def release_notification(ids: list[int]) -> None:
    from sqlalchemy import update

    with session_scope() as s:
        s.execute(update(Application).where(Application.id.in_(ids)).values(notified_at=None))


def pending_updates(s: Session, user_id: int) -> dict[str, Any]:
    """What the bot should tell this user: pause, applications to confirm, fresh results."""
    cfg = get_config(s, user_id)
    base = select(Application).where(Application.user_id == user_id, Application.notified_at.is_(None))
    to_confirm = []
    if cfg.enabled and cfg.mode == "confirm" and not cfg.paused_reason:
        to_confirm = list(s.scalars(base.where(Application.status == ApplicationStatus.queued)
                                    .order_by(Application.score.desc()).limit(5)))
    results = list(s.scalars(base.where(Application.status.in_(
        [ApplicationStatus.applied, ApplicationStatus.failed, ApplicationStatus.skipped]))
        .order_by(Application.id).limit(10)))
    pause_note = cfg.paused_reason if (cfg.enabled and cfg.paused_reason and not cfg.paused_notified) else ""
    return {"to_confirm": to_confirm, "results": results, "pause_note": pause_note}


def mark_pause_notified(s: Session, user_id: int) -> None:
    get_config(s, user_id).paused_notified = True
