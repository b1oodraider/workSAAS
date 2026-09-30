"""Auto-apply: pick the best matches, write a letter, send ONE application at a time.

Guard rails (never exceeded, whatever the user sets or had set before an admin lowered them):
- at most `apply.hard_daily_max` applications per user per day, and the user's own limit;
- at least `apply.min_interval_floor_s` between applications (plus jitter), counted from the
  moment an application actually went out;
- only within the user's active hours (local time);
- only vacancies with a match score >= min_score and recommendation != "skip",
  without serious red flags in the vacancy review, still open in the user's tracker;
- letters are written ahead of time without the user's private preferences; a letter with
  links, contacts or unusual length waits for the user ("review") even in auto mode;
- anything that looks like a captcha/logout ("blocked"), a spent budget or several
  failures in a row pauses auto-apply until the user presses «Продолжить»;
- one browser at a time per process, so several accounts never apply simultaneously.
In "confirm" mode nothing is sent before the user approves it (web page or Telegram).

Flow: `autoapply_tick` (every scheduler tick) recovers stuck rows, tops up the queue,
starts `autoapply_prepare` (letters) and at most one `autoapply_send` per user.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.apply import ApplyRequest, applier_for, supported_sources
from app.core.config import get_settings
from app.core.db import from_local, session_scope, to_local, utcnow
from app.core.errors import NotFound, UserError, ValidationFailed
from app.features import get_feature
from app.jobs import JobContext, enqueue, job_handler
from app.jobs.scheduler import periodic
from app.llm import BudgetExceeded
from app.models import (
    ACTIVE_APPLICATION_STATUSES,
    Analysis,
    Application,
    ApplicationStatus,
    AutoApplySettings,
    Job,
    JobStatus,
    User,
    UserVacancy,
    Vacancy,
)
from app.services import analysis as analysis_svc
from app.services import matches as matches_svc
from app.services import resumes as resume_svc
from app.services import vacancies as vacancy_svc

log = logging.getLogger(__name__)

MODES = ("confirm", "auto")
MAX_TRANSIENT_ATTEMPTS = 3
LETTERS_PER_PREPARE_JOB = 5
# A "sending" row without a live job older than this was interrupted (crash, restart).
STUCK_AFTER = timedelta(minutes=15)
MAX_LETTER_CHARS = 2500

# Keys read from other features' outputs (cross-feature data comes from the analyses table).
MATCH_RECOMMENDATION = "recommendation"
REVIEW_RED_FLAGS, RED_FLAG_SEVERITY = "red_flags", "severity"
LETTER_BODY = "body"

# Only one automation browser per process: several accounts applying at the same second
# from one IP is an easy pattern for the site to link.
_BROWSER_LOCK = asyncio.Lock()


def tone_labels() -> dict[str, str]:
    """Letter tones as the cover_letter feature defines them (single source of truth)."""
    field = get_feature("cover_letter").params_model.model_fields["tone"]
    extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
    return dict(extra.get("labels") or {})


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
                  resume_id: int | None, site_resume_title: str, letter_tone: str) -> AutoApplySettings:
    guard = get_settings().apply
    if mode not in MODES:
        raise ValidationFailed("Неизвестный режим")
    if letter_tone not in tone_labels():
        raise ValidationFailed("Неизвестный тон письма")
    if not (0 <= active_from_hour < active_to_hour <= 24):
        raise ValidationFailed("Часы работы: «с» должно быть меньше «до», в пределах 0–24")
    if resume_id is not None:
        resume_svc.get_owned(s, user_id, resume_id)
    cfg = get_config(s, user_id)
    was_enabled = cfg.enabled
    cfg.enabled = enabled
    cfg.mode = mode
    cfg.min_score = max(50, min(int(min_score), 100))
    cfg.daily_limit = max(1, min(int(daily_limit), guard.hard_daily_max))
    cfg.min_interval_s = max(guard.min_interval_floor_s, int(min_interval_s))
    cfg.active_from_hour, cfg.active_to_hour = active_from_hour, active_to_hour
    cfg.resume_id = resume_id
    cfg.site_resume_title = site_resume_title.strip()[:200]
    cfg.letter_tone = letter_tone
    if enabled and not was_enabled:
        # Switching on is a fresh start. Saving other settings never lifts a kill-switch pause:
        # only «Продолжить» does, so a captcha isn't hit again by accident.
        _clear_pause(cfg)
    return cfg


def _clear_pause(cfg: AutoApplySettings) -> None:
    cfg.paused_reason, cfg.paused_notified, cfg.consecutive_failures = "", False, 0


def pause(s: Session, user_id: int, reason: str) -> None:
    cfg = get_config(s, user_id)
    cfg.paused_reason = reason[:500] or "пауза"
    cfg.paused_notified = False


def resume_after_pause(s: Session, user_id: int) -> None:
    _clear_pause(get_config(s, user_id))


def is_running(cfg: AutoApplySettings) -> bool:
    return bool(cfg.enabled and not cfg.paused_reason and get_settings().apply.enabled)


def effective_limits(cfg: AutoApplySettings) -> tuple[int, int]:
    """(daily limit, min interval in seconds) after the server-wide caps."""
    guard = get_settings().apply
    return min(cfg.daily_limit, guard.hard_daily_max), max(cfg.min_interval_s, guard.min_interval_floor_s)


# --------------------------------------------------------------------------- selection


def _day_start_utc(now: datetime) -> datetime:
    """Midnight of the user's local day, in UTC (daily limits follow the user's calendar)."""
    local = to_local(now)
    return from_local(local.replace(hour=0, minute=0, second=0, microsecond=0))


def sent_today(s: Session, user_id: int, now: datetime | None = None) -> int:
    """Applications that went out (or may have) since local midnight."""
    start = _day_start_utc(now or utcnow())
    return s.scalar(select(func.count(Application.id)).where(
        Application.user_id == user_id, Application.sent_at >= start,
        Application.status.in_([ApplicationStatus.applied, ApplicationStatus.sending,
                                ApplicationStatus.failed]))) or 0


def _has_serious_red_flags(s: Session, user_id: int, vacancy_id: int) -> bool:
    review = analysis_svc.latest(s, user_id, "vacancy_review", vacancy_id=vacancy_id)
    flags = (review.output or {}).get(REVIEW_RED_FLAGS) if review else None
    return any(isinstance(f, dict) and f.get(RED_FLAG_SEVERITY) == "high" for f in flags or [])


def _still_wanted(s: Session, user_id: int, vacancy_id: int) -> bool:
    status = s.scalar(select(UserVacancy.status).where(UserVacancy.user_id == user_id,
                                                       UserVacancy.vacancy_id == vacancy_id))
    return status is not None and getattr(status, "value", status) in matches_svc.OPEN_STATUSES


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
        if (a.output or {}).get(MATCH_RECOMMENDATION) == "skip":
            continue
        if _has_serious_red_flags(s, user_id, v.id):
            continue
        result.append((v, a))
    return result


def plan(s: Session, user_id: int) -> list[int]:
    """Top up the queue so that waiting + sent today never exceeds the daily limit."""
    cfg = get_config(s, user_id)
    daily, _ = effective_limits(cfg)
    waiting = s.scalar(select(func.count(Application.id)).where(
        Application.user_id == user_id,
        Application.status.in_([ApplicationStatus.queued, ApplicationStatus.review,
                                ApplicationStatus.approved]))) or 0
    room = daily - sent_today(s, user_id) - waiting
    if room <= 0:
        return []
    added = []
    for v, a in candidates(s, user_id, cfg)[:room]:
        try:
            with s.begin_nested():  # a concurrent plan() may have taken the same vacancy
                app = Application(user_id=user_id, vacancy_id=v.id, resume_id=a.resume_id, score=a.score)
                s.add(app)
                s.flush()
        except IntegrityError:
            continue
        added.append(app.id)
    return added


# --------------------------------------------------------------------------- letters


_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{8,}\d")
_TAG_RE = re.compile(r"</?(?:resume|vacancy|preferences|user_text|match_analysis)\b", re.I)


def letter_problems(letter: str, resume_text: str) -> list[str]:
    """Why a generated letter should not go out unseen. Contacts and links are allowed only if
    they come from the resume itself — anything else may be injected by the vacancy text."""
    problems = []
    if not letter.strip():
        problems.append("письмо пустое")
    if len(letter) > MAX_LETTER_CHARS:
        problems.append(f"письмо слишком длинное ({len(letter)} символов)")
    resume_lower = resume_text.lower()
    resume_digits = re.sub(r"\D", "", resume_text)
    if any(m.group(0).rstrip(".,;)").lower() not in resume_lower for m in _URL_RE.finditer(letter)):
        problems.append("в письме есть ссылка, которой нет в резюме")
    if any(m.group(0).rstrip(".").lower() not in resume_lower for m in _EMAIL_RE.finditer(letter)):
        problems.append("в письме есть e-mail, которого нет в резюме")
    if any(re.sub(r"\D", "", m.group(0)) not in resume_digits for m in _PHONE_RE.finditer(letter)):
        problems.append("в письме есть номер телефона, которого нет в резюме")
    if _TAG_RE.search(letter):
        problems.append("в письме остались служебные метки")
    return problems


def _active_jobs(s: Session, user_id: int, kind: str) -> list[Job]:
    return list(s.scalars(select(Job).where(Job.user_id == user_id, Job.kind == kind,
                                            Job.status.in_([JobStatus.queued, JobStatus.running]))))


def _needs_letters(s: Session, user_id: int) -> bool:
    return bool(s.scalar(select(func.count(Application.id)).where(
        Application.user_id == user_id, Application.letter == "",
        Application.status.in_([ApplicationStatus.queued, ApplicationStatus.approved]))))


BUDGET_REASON = ("закончился месячный лимит на ИИ — письма писать не на что. Посмотрите «Расходы»; "
                 "продолжите, когда лимит увеличат или начнётся новый месяц")


@job_handler("autoapply_prepare")
async def _prepare_job(ctx: JobContext) -> dict:
    """Write letters for waiting applications, best first (a few per job)."""
    with session_scope() as s:
        cfg = get_config(s, ctx.user_id)
        if not is_running(cfg):
            return {"skipped": "auto-apply is not running"}
        tone = cfg.letter_tone
        todo = [(a.id, a.resume_id, a.vacancy_id) for a in s.scalars(
            select(Application).where(
                Application.user_id == ctx.user_id, Application.letter == "",
                Application.status.in_([ApplicationStatus.queued, ApplicationStatus.approved]))
            .order_by(Application.score.desc().nullslast(), Application.id).limit(LETTERS_PER_PREPARE_JOB))]
    written = 0
    for app_id, resume_id, vacancy_id in todo:
        try:
            analysis = await analysis_svc.run_analysis(
                ctx.user_id, "cover_letter", resume_id=resume_id, vacancy_id=vacancy_id,
                params={"tone": tone, "length": "short", "use_preferences": False})
        except BudgetExceeded:
            with session_scope() as s:
                pause(s, ctx.user_id, BUDGET_REASON)
            return {"status": "paused", "reason": "budget", "written": written}
        except UserError as exc:
            if exc.retryable and not ctx.final_attempt:
                raise  # the job queue retries it
            with session_scope() as s:
                app = s.get(Application, app_id)
                if app is not None and app.status in (ApplicationStatus.queued, ApplicationStatus.approved):
                    app.status = ApplicationStatus.failed
                    app.reason = f"не удалось написать письмо: {exc}"[:1000]
                    app.notified_at = None
            continue
        letter = str((analysis.output or {}).get(LETTER_BODY, "")).strip()
        with session_scope() as s:
            app = s.get(Application, app_id)
            if app is None or app.status not in (ApplicationStatus.queued, ApplicationStatus.approved):
                continue  # cancelled meanwhile
            resume = resume_svc.get_owned(s, ctx.user_id, resume_id) if resume_id else None
            problems = letter_problems(letter, resume.text if resume else "")
            app.letter = letter
            if problems:
                app.status = ApplicationStatus.review
                app.reason = "проверьте письмо: " + "; ".join(problems)
                app.notified_at = None
            written += 1
    return {"written": written, "result_url": "/autoapply"}


# --------------------------------------------------------------------------- sending


def _jittered_interval(interval_s: int, app_id: int) -> timedelta:
    # Deterministic 0-50% jitter so applications don't go out like clockwork.
    return timedelta(seconds=interval_s * (1 + (app_id * 37 % 50) / 100))


def _ready_statuses(cfg: AutoApplySettings) -> list[ApplicationStatus]:
    if cfg.mode == "confirm":
        return [ApplicationStatus.approved]
    return [ApplicationStatus.queued, ApplicationStatus.approved]


def _in_active_hours(cfg: AutoApplySettings, now: datetime) -> bool:
    return cfg.active_from_hour <= to_local(now).hour < cfg.active_to_hour


def next_to_send(s: Session, user_id: int, now: datetime | None = None) -> Application | None:
    """The application that may be sent right now, or None (limits, hours, nothing ready)."""
    now = now or utcnow()
    cfg = get_config(s, user_id)
    if not is_running(cfg) or not _in_active_hours(cfg, now):
        return None
    daily, interval_s = effective_limits(cfg)
    if sent_today(s, user_id, now) >= daily:
        return None
    if s.scalar(select(func.count(Application.id)).where(
            Application.user_id == user_id, Application.status == ApplicationStatus.sending)):
        return None
    app = s.scalar(select(Application).where(
        Application.user_id == user_id, Application.status.in_(_ready_statuses(cfg)), Application.letter != "")
        .order_by(Application.score.desc().nullslast(), Application.id).limit(1))
    if app is None:
        return None
    last = s.scalar(select(func.max(Application.sent_at)).where(Application.user_id == user_id))
    if last and last + _jittered_interval(interval_s, app.id) > now:
        return None
    return app


def _recover_stuck(s: Session, user_id: int, now: datetime) -> None:
    """'sending' rows whose job is gone (crash, restart) must not block the user forever."""
    stuck = list(s.scalars(select(Application).where(
        Application.user_id == user_id, Application.status == ApplicationStatus.sending)))
    if not stuck:
        return
    live = {int((j.payload or {}).get("application_id", 0)) for j in _active_jobs(s, user_id, "autoapply_send")}
    for app in stuck:
        if app.id in live or (app.sent_at and app.sent_at + STUCK_AFTER > now):
            continue
        # We can't know whether the site got it: count it against the limits and tell the user.
        app.status = ApplicationStatus.failed
        app.reason = "отправка прервалась (перезапуск сервера?) — проверьте на сайте, ушёл ли отклик"
        app.sent_at = app.sent_at or now
        app.notified_at = None


def _tick_user(user_id: int, now: datetime) -> int | None:
    with session_scope() as s:
        if not is_running(get_config(s, user_id)):
            return None
        _recover_stuck(s, user_id, now)
        plan(s, user_id)
        wants_letters = _needs_letters(s, user_id) and not _active_jobs(s, user_id, "autoapply_prepare")
    if wants_letters:
        enqueue("autoapply_prepare", {}, user_id=user_id, title="Автоотклики: письма")
    with session_scope() as s:
        app = next_to_send(s, user_id, now)
        if app is None:
            return None
        # Claim atomically: a web/bot cancel or a second scheduler may have touched it meanwhile.
        claimed = s.execute(update(Application).where(Application.id == app.id, Application.status == app.status)
                            .values(status=ApplicationStatus.sending, sent_at=now)).rowcount
        if not claimed:
            return None
        app_id = app.id
    enqueue("autoapply_send", {"application_id": app_id}, user_id=user_id, title="Автоотклик")
    return app_id


@periodic
def autoapply_tick(now: datetime | None = None) -> list[int]:
    """Scheduler tick: recover, plan, prepare letters, start at most one sending job per user."""
    now = now or utcnow()
    started = []
    with session_scope() as s:
        user_ids = list(s.scalars(
            select(AutoApplySettings.user_id).join(User, User.id == AutoApplySettings.user_id)
            .where(AutoApplySettings.enabled.is_(True), AutoApplySettings.paused_reason == "",
                   User.is_active.is_(True))))
    for user_id in user_ids:
        try:
            app_id = _tick_user(user_id, now)
        except Exception:  # noqa: BLE001 - one user's bad data must not stop everyone else
            log.exception("autoapply tick failed for user %s", user_id)
            continue
        if app_id is not None:
            started.append(app_id)
    return started


def _finish(app_id: int, status: ApplicationStatus, reason: str, *, sent_at: datetime | None) -> None:
    with session_scope() as s:
        app = s.get(Application, app_id)
        app.status = status
        app.reason = reason[:1000]
        app.sent_at = sent_at  # None: nothing went out, don't count it against limits/interval
        app.notified_at = None


def _count_failure(user_id: int) -> None:
    with session_scope() as s:
        cfg = get_config(s, user_id)
        cfg.consecutive_failures += 1
        limit = get_settings().apply.max_consecutive_failures
        if cfg.consecutive_failures >= limit and not cfg.paused_reason:
            pause(s, user_id, f"несколько неудачных откликов подряд ({cfg.consecutive_failures}) — посмотрите "
                              "причины в истории; возможно, сайт изменился или нужно войти заново")


def _precheck(user_id: int, vacancy_id: int) -> str:
    """Re-check right before sending: things may have changed while the job waited in line."""
    with session_scope() as s:
        user = s.get(User, user_id)
        cfg = get_config(s, user_id)
        if user is None or not user.is_active or not is_running(cfg):
            return "wait"
        if not _in_active_hours(cfg, utcnow()):
            return "wait"
        if not _still_wanted(s, user_id, vacancy_id):
            return "cancel"
    return ""


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
        req = ApplyRequest(user_id=ctx.user_id, source=vacancy.source, external_id=vacancy.external_id,
                           url=vacancy.url, letter=app.letter, site_resume_title=cfg.site_resume_title)
        back = ApplicationStatus.approved if cfg.mode == "confirm" else ApplicationStatus.queued
        vacancy_id, attempts = app.vacancy_id, app.attempts

    if applier is None:
        _finish(app_id, ApplicationStatus.skipped, "для этого сайта автоотклик не поддерживается", sent_at=None)
        return {"status": "skipped"}
    if not req.letter:
        _finish(app_id, back, "", sent_at=None)  # the letter isn't written yet
        return {"status": "waiting"}
    check = _precheck(ctx.user_id, vacancy_id)
    if check == "wait":
        _finish(app_id, back, "", sent_at=None)
        return {"status": "waiting"}
    if check == "cancel":
        _finish(app_id, ApplicationStatus.cancelled, "вакансия уже разобрана в трекере", sent_at=None)
        return {"status": "cancelled"}

    try:
        async with _BROWSER_LOCK:
            result = await applier.apply(req)
    except Exception:  # noqa: BLE001 - never leave the row in 'sending'
        log.exception("applier crashed for application %s", app_id)
        _finish(app_id, ApplicationStatus.failed, "внутренняя ошибка при отправке — проверьте на сайте, "
                                                   "ушёл ли отклик", sent_at=utcnow())
        _count_failure(ctx.user_id)
        return {"status": "failed"}

    now = utcnow()
    if result.status == "applied":
        _finish(app_id, ApplicationStatus.applied, result.reason, sent_at=now)
        with session_scope() as s:
            get_config(s, ctx.user_id).consecutive_failures = 0
            try:
                vacancy_svc.set_status(s, ctx.user_id, vacancy_id, "applied")
            except NotFound:
                pass
    elif result.status == "skipped":
        _finish(app_id, ApplicationStatus.skipped, result.reason, sent_at=None)
    elif result.status == "blocked":
        if result.maybe_sent:
            _finish(app_id, ApplicationStatus.failed, result.reason, sent_at=now)
        else:  # nothing was sent: put it back in line and stop until the user looks at it
            _finish(app_id, back, result.reason, sent_at=None)
        with session_scope() as s:
            pause(s, ctx.user_id, result.reason)
    elif result.transient and attempts + 1 < MAX_TRANSIENT_ATTEMPTS:
        # Back in line; keeping sent_at makes the next try wait for the usual interval.
        with session_scope() as s:
            app = s.get(Application, app_id)
            app.status, app.reason, app.sent_at, app.attempts = back, result.reason, now, attempts + 1
    else:
        _finish(app_id, ApplicationStatus.failed, result.reason, sent_at=now if result.maybe_sent else None)
        _count_failure(ctx.user_id)
    return {"status": result.status, "reason": result.reason, "result_url": "/autoapply"}


# --------------------------------------------------------------------------- user actions


def get_owned(s: Session, user_id: int, app_id: int) -> Application:
    app = s.get(Application, app_id)
    if app is None or app.user_id != user_id:
        raise NotFound("application")
    return app


def approve(s: Session, user_id: int, app_id: int) -> str:
    """Returns a short human answer for the web flash / Telegram toast."""
    app = get_owned(s, user_id, app_id)
    if app.status not in (ApplicationStatus.queued, ApplicationStatus.review):
        return "Этот отклик уже не ждёт решения"
    app.status = ApplicationStatus.approved
    app.reason = ""
    cfg = get_config(s, user_id)
    if not is_running(cfg):
        return "Подтверждено. Автоотклики сейчас не работают — отправлю, когда вы их продолжите"
    return f"Подтверждено — отправлю в ближайшее разрешённое время ({cfg.active_from_hour}:00–{cfg.active_to_hour}:00)"


def cancel(s: Session, user_id: int, app_id: int) -> str:
    app = get_owned(s, user_id, app_id)
    if app.status not in (ApplicationStatus.queued, ApplicationStatus.review, ApplicationStatus.approved):
        return "Этот отклик уже не в очереди"
    app.status = ApplicationStatus.cancelled
    return "Не откликаемся на эту вакансию"


def restore(s: Session, user_id: int, app_id: int) -> str:
    """Undo a «не откликаться»."""
    app = get_owned(s, user_id, app_id)
    if app.status != ApplicationStatus.cancelled:
        return "Этот отклик нельзя вернуть"
    app.status = ApplicationStatus.queued
    app.notified_at = utcnow()  # the user is looking at it right now: don't ask again
    return "Вернул в очередь"


def site_readiness(user_id: int) -> dict[str, dict[str, Any]]:
    readiness = {}
    for source in supported_sources():
        applier = applier_for(source)
        ok, hint = applier.is_ready(user_id)
        readiness[source] = {"ok": ok, "hint": hint, "title": applier.title}
    return readiness


def idle_reason(s: Session, user_id: int, readiness: dict[str, dict[str, Any]] | None = None,
                now: datetime | None = None) -> str:
    """Why nothing is being sent right now ("" when auto-apply is actually working)."""
    now = now or utcnow()
    cfg = get_config(s, user_id)
    if not get_settings().apply.enabled:
        return "автоотклики выключены администратором"
    if not cfg.enabled:
        return "автоотклики выключены"
    if cfg.paused_reason:
        return f"на паузе: {cfg.paused_reason}"
    readiness = readiness if readiness is not None else site_readiness(user_id)
    if readiness and not any(r["ok"] for r in readiness.values()):
        return next(iter(readiness.values()))["hint"]
    daily, _ = effective_limits(cfg)
    if sent_today(s, user_id, now) >= daily:
        return "лимит на сегодня исчерпан — продолжу завтра"
    if not _in_active_hours(cfg, now):
        return f"сейчас нерабочее время — начну в {cfg.active_from_hour}:00"
    return ""


def awaiting_decision(cfg: AutoApplySettings, app: Application) -> bool:
    """Is this application waiting for the user (approve / skip)?"""
    if app.status == ApplicationStatus.review:
        return True
    return cfg.mode == "confirm" and app.status == ApplicationStatus.queued and bool(app.letter)


def overview(s: Session, user_id: int) -> dict[str, Any]:
    cfg = get_config(s, user_id)
    apps = list(s.scalars(select(Application).where(Application.user_id == user_id)
                          .order_by(Application.id.desc()).limit(200)))
    queue = sorted((a for a in apps if a.status in ACTIVE_APPLICATION_STATUSES),
                   key=lambda a: (-(a.score or 0), a.id))
    readiness = site_readiness(user_id)
    daily, interval_s = effective_limits(cfg)
    return {
        "cfg": cfg,
        "running": is_running(cfg),
        "idle_reason": idle_reason(s, user_id, readiness),
        "queue": queue,
        "awaiting": sum(1 for a in queue if awaiting_decision(cfg, a)),
        "history": [a for a in apps if a.status not in ACTIVE_APPLICATION_STATUSES],
        "sent_today": sent_today(s, user_id),
        "daily_limit": daily,
        "interval_min": max(1, interval_s // 60),
        "hard_daily_max": get_settings().apply.hard_daily_max,
        "min_interval_floor_s": get_settings().apply.min_interval_floor_s,
        "server_enabled": get_settings().apply.enabled,
        "readiness": readiness,
        "resumes": resume_svc.list_for_user(s, user_id),
        "tones": tone_labels(),
    }


# --------------------------------------------------------------------------- site sessions


def _applier_or_404(site: str):
    applier = applier_for(site)
    if applier is None:
        raise NotFound("site")
    return applier


def save_site_session(s: Session, user_id: int, site: str, data: bytes) -> bool:
    """Store an uploaded login session. Returns True if this also lifted a pause (fresh login)."""
    _applier_or_404(site).save_session_bytes(user_id, data)
    cfg = get_config(s, user_id)
    if cfg.enabled and cfg.paused_reason:
        resume_after_pause(s, user_id)
        return True
    return False


def delete_site_session(user_id: int, site: str) -> None:
    _applier_or_404(site).delete_session(user_id)


def forget_all_sessions(user_id: int) -> None:
    """Drop every saved site login of a user (e.g. when an admin deactivates the account)."""
    for source in supported_sources():
        applier_for(source).delete_session(user_id)


# --------------------------------------------------------------------------- notifications (bot)


def claim_for_notification(ids: list[int]) -> list[int]:
    """Mark applications as reported; returns ids this process won (safe with two bot processes)."""
    won = []
    with session_scope() as s:
        for app_id in ids:
            res = s.execute(update(Application).where(Application.id == app_id, Application.notified_at.is_(None))
                            .values(notified_at=utcnow()))
            if res.rowcount:
                won.append(app_id)
    return won


def release_notification(ids: list[int]) -> None:
    with session_scope() as s:
        s.execute(update(Application).where(Application.id.in_(ids)).values(notified_at=None))


def pending_updates(s: Session, user_id: int) -> dict[str, Any]:
    """What the bot should tell this user: applications to decide on and fresh results."""
    cfg = get_config(s, user_id)
    base = select(Application).where(Application.user_id == user_id, Application.notified_at.is_(None))
    to_confirm = []
    if cfg.enabled and not cfg.paused_reason:
        # Ask only once the letter is written, so the user approves the exact text.
        wanted = [ApplicationStatus.review]
        if cfg.mode == "confirm":
            wanted.append(ApplicationStatus.queued)
        to_confirm = list(s.scalars(base.where(Application.status.in_(wanted), Application.letter != "")
                                    .order_by(Application.score.desc().nullslast()).limit(5)))
    results = list(s.scalars(base.where(Application.status.in_(
        [ApplicationStatus.applied, ApplicationStatus.failed, ApplicationStatus.skipped]))
        .order_by(Application.id).limit(10)))
    return {"to_confirm": to_confirm, "results": results}


def claim_pause_notice(user_id: int) -> str:
    """Atomically take the pause notice to send ("" if there is none or another process took it)."""
    with session_scope() as s:
        cfg = get_config(s, user_id)
        if not (cfg.enabled and cfg.paused_reason) or cfg.paused_notified:
            return ""
        reason = cfg.paused_reason
        won = s.execute(update(AutoApplySettings).where(AutoApplySettings.user_id == user_id,
                                                        AutoApplySettings.paused_notified.is_(False))
                        .values(paused_notified=True)).rowcount
        return reason if won else ""


def release_pause_notice(user_id: int) -> None:
    with session_scope() as s:
        get_config(s, user_id).paused_notified = False


def mark_pause_notified(s: Session, user_id: int) -> None:
    get_config(s, user_id).paused_notified = True
