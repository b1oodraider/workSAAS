"""Tracker sync with job sites: statuses of applications ("viewed", "invitation", "rejection")
and applications the user made by hand on the site.

Every `[apply] status_sync_hours` the scheduler starts a `site_sync` job for each user with a
working saved session; the job reads the site's list of applications (read-only) and moves
tracker statuses forward. It never moves a status back and never touches an offer.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.apply.base import SessionExpired, SiteApplication
from app.core.config import get_settings
from app.core.db import session_scope, utcnow
from app.jobs import JobContext, enqueue, job_handler
from app.jobs.scheduler import periodic
from app.models import Job, JobStatus, User, UserVacancy, UserVacancyStatus, Vacancy
from app.services import vacancies as vacancy_svc
from app.sources import VacancyDraft

log = logging.getLogger(__name__)

# How far along the funnel a status is; the site may only move a vacancy forward.
_RANK = {"new": 0, "saved": 1, "hidden": 1, "applied": 2, "interview": 3, "rejected": 4, "offer": 5}
_TARGET = {"applied": "applied", "viewed": "applied", "invited": "interview", "rejected": "rejected"}


def _ready_appliers(user_id: int) -> list:
    from app.apply.registry import APPLIERS  # looked up at call time: tests replace entries

    return [a for a in APPLIERS.values() if a.session_ok(user_id)]


@periodic
def enqueue_due_syncs() -> list[int]:
    hours = get_settings().apply.status_sync_hours
    if hours <= 0:
        return []
    cutoff = utcnow() - timedelta(hours=hours)
    with session_scope() as s:
        user_ids = list(s.scalars(select(User.id).where(User.is_active.is_(True))))
        last = dict(s.execute(select(Job.user_id, func.max(Job.created_at))
                              .where(Job.kind == "site_sync").group_by(Job.user_id)).all())
    return [enqueue_sync(uid) for uid in user_ids
            if _ready_appliers(uid) and (last.get(uid) is None or last[uid] < cutoff)]


MANUAL_MIN_INTERVAL = timedelta(minutes=15)


def enqueue_sync(user_id: int, *, manual: bool = False) -> int:
    """A new sync job, or the one already waiting/running or just made (no hammering hh.ru)."""
    with session_scope() as s:
        last = last_sync(s, user_id)
        if last is not None and (last.status in (JobStatus.queued, JobStatus.running)
                                 or (manual and last.created_at > utcnow() - MANUAL_MIN_INTERVAL)):
            return last.id
    return enqueue("site_sync", {}, user_id=user_id, title="Отклики: статусы с сайтов", result_url="/tracker")


def can_sync(user_id: int) -> bool:
    return bool(_ready_appliers(user_id))


def last_sync(s: Session, user_id: int) -> Job | None:
    return s.scalar(select(Job).where(Job.user_id == user_id, Job.kind == "site_sync")
                    .order_by(Job.id.desc()).limit(1))


def _tracked_copy(s: Session, user_id: int, vacancy: Vacancy) -> Vacancy:
    if vacancy.dedup_key:
        tracked = s.scalar(select(Vacancy).join(UserVacancy, UserVacancy.vacancy_id == Vacancy.id).where(
            UserVacancy.user_id == user_id, Vacancy.dedup_key == vacancy.dedup_key).order_by(Vacancy.id).limit(1))
        if tracked is not None:
            return tracked
    return vacancy_svc.canonical_for(s, vacancy)


def apply_statuses(s: Session, user_id: int, source: str, items: list[SiteApplication]) -> dict[str, int]:
    """Move tracker statuses forward from the site's list. Returns counts for the job result."""
    counts = {"added": 0, "updated": 0, "invited": 0, "rejected": 0}
    for item in items:
        vacancy = s.scalar(select(Vacancy).where(Vacancy.source == source, Vacancy.external_id == item.external_id))
        if vacancy is None:
            if item.status == "archived":
                continue
            # Applied by hand on the site: put it into the tracker (the full card loads on demand).
            vacancy, _ = vacancy_svc.upsert(s, VacancyDraft(source=source, external_id=item.external_id,
                                                            title=item.title, company=item.company,
                                                            url=item.url, is_partial=True))
            counts["added"] += 1
        # The user may track this vacancy under another board's copy (search keeps one copy per
        # dedup key): update that card instead of adding a duplicate.
        vacancy = _tracked_copy(s, user_id, vacancy)
        uv = vacancy_svc.attach(s, user_id, vacancy.id)
        target = _TARGET.get(item.status)
        if target is None or _RANK[target] <= _RANK[uv.status.value]:
            continue
        vacancy_svc.set_status(s, user_id, vacancy.id, target)
        counts["updated"] += 1
        if target == UserVacancyStatus.interview.value:
            counts["invited"] += 1
        elif target == UserVacancyStatus.rejected.value:
            counts["rejected"] += 1
    return counts


@job_handler("site_sync")
async def _sync_job(ctx: JobContext) -> dict:
    totals = {"added": 0, "updated": 0, "invited": 0, "rejected": 0}
    problems: list[str] = []
    for applier in _ready_appliers(ctx.user_id):
        try:
            items = await applier.site_applications(ctx.user_id)
        except SessionExpired as exc:
            applier.mark_session_stale(ctx.user_id)
            problems.append(f"{applier.title}: {exc}")
            continue
        except httpx.HTTPError as exc:
            log.warning("site sync %s for user %s failed: %s", applier.source, ctx.user_id, exc)
            problems.append(f"{applier.title}: сайт не ответил — попробуем позже")
            continue
        if items is None:
            continue
        with session_scope() as s:
            for key, n in apply_statuses(s, ctx.user_id, applier.source, items).items():
                totals[key] += n
    return {**totals, "problems": problems, "result_url": "/tracker"}
