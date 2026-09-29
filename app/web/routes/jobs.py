from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Job, JobStatus
from app.services.errors import NotFound
from app.web.deps import CurrentUser, current_user, db
from app.web.templating import render

router = APIRouter(prefix="/jobs")


def _get(s: Session, user: CurrentUser, job_id: int) -> Job:
    job = s.get(Job, job_id)
    if job is None or (job.user_id != user.id and not user.is_admin):
        raise NotFound("job")
    return job


def _children_summary(s: Session, job_id: int) -> dict[str, int]:
    rows = s.execute(
        select(Job.status, func.count()).where(Job.parent_id == job_id).group_by(Job.status)
    ).all()
    return {status.value: count for status, count in rows}


@router.get("")
def list_page(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    jobs = list(s.scalars(
        select(Job).where(Job.user_id == user.id).order_by(Job.id.desc()).limit(100)
    ))
    return render(request, "jobs/list.html", jobs=jobs)


@router.get("/{job_id}")
def detail(job_id: int, request: Request, user: CurrentUser = Depends(current_user),
           s: Session = Depends(db)):
    job = _get(s, user, job_id)
    children = _children_summary(s, job.id)
    pending_children = children.get("queued", 0) + children.get("running", 0)
    # Single-step jobs jump straight to the result.
    if job.status == JobStatus.done and job.result_url and not children:
        return RedirectResponse(job.result_url, status_code=303)
    return render(request, "jobs/detail.html", job=job, children=children,
                  pending_children=pending_children)


@router.get("/{job_id}/status")
def status(job_id: int, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    job = _get(s, user, job_id)
    children = _children_summary(s, job.id)
    return JSONResponse({
        "status": job.status.value,
        "error": job.error,
        "result_url": job.result_url,
        "children": children,
    })
