from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import from_local
from app.services import site_sync
from app.services import vacancies as vacancy_svc
from app.services.errors import ValidationFailed
from app.web.deps import CurrentUser, current_user, db, safe_path
from app.models import STATUS_LABELS
from app.web.templating import flash, render

router = APIRouter()


@router.get("/tracker")
def tracker_page(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db, scope="function")):
    return render(request, "tracker.html", data=vacancy_svc.tracker(s, user.id), statuses=STATUS_LABELS,
                  can_sync=site_sync.can_sync(user.id), last_sync=site_sync.last_sync(s, user.id))


@router.post("/tracker/sync")
def sync_now(request: Request, user: CurrentUser = Depends(current_user)):
    if not site_sync.can_sync(user.id):
        flash(request, "Сначала войдите в hh.ru на странице «Автоотклики»", "error")
        return RedirectResponse("/tracker", status_code=303)
    job_id = site_sync.enqueue_sync(user.id, manual=True)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/vacancies/{vacancy_id}/next-action")
def next_action(vacancy_id: int, request: Request, when: str = Form(""), note: str = Form(""),
                back: str = Form(""), user: CurrentUser = Depends(current_user), s: Session = Depends(db, scope="function")):
    try:
        moment = from_local(datetime.fromisoformat(when)) if when else None
    except ValueError:
        flash(request, "Некорректная дата", "error")
        return RedirectResponse(safe_path(back, f"/vacancies/{vacancy_id}"), status_code=303)
    try:
        vacancy_svc.set_next_action(s, user.id, vacancy_id, moment, note)
        flash(request, "Сохранено" if moment else "Напоминание снято")
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(safe_path(back, f"/vacancies/{vacancy_id}"), status_code=303)
