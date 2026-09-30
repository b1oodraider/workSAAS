from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.services import autoapply as autoapply_svc
from app.web.deps import CurrentUser, current_user, db
from app.web.templating import flash, render

router = APIRouter(prefix="/autoapply")

MODE_LABELS = {"confirm": "спрашивать перед каждым откликом", "auto": "откликаться автоматически"}
TONE_LABELS = {"friendly": "дружелюбный деловой", "formal": "официальный", "concise": "кратко",
               "enthusiastic": "с энтузиазмом"}
APP_STATUS_LABELS = {"queued": "в очереди", "approved": "одобрен", "sending": "отправляется",
                     "applied": "отправлен", "skipped": "пропущен", "failed": "ошибка", "cancelled": "отменён"}


def _back() -> RedirectResponse:
    return RedirectResponse("/autoapply", status_code=303)


@router.get("")
def page(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    return render(request, "autoapply.html", data=autoapply_svc.overview(s, user.id), modes=MODE_LABELS,
                  tones=TONE_LABELS, app_status=APP_STATUS_LABELS)


@router.post("/settings")
def save_settings(request: Request, enabled: bool = Form(False), mode: str = Form("confirm"),
                  min_score: int = Form(80), daily_limit: int = Form(10), min_interval_min: int = Form(2),
                  active_from_hour: int = Form(9), active_to_hour: int = Form(21), resume_id: str = Form(""),
                  hh_resume_title: str = Form(""), letter_tone: str = Form("friendly"),
                  user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    try:
        autoapply_svc.update_config(
            s, user.id, enabled=enabled, mode=mode, min_score=min_score, daily_limit=daily_limit,
            min_interval_s=min_interval_min * 60, active_from_hour=active_from_hour,
            active_to_hour=active_to_hour, resume_id=int(resume_id) if resume_id.isdigit() else None,
            hh_resume_title=hh_resume_title, letter_tone=letter_tone)
        flash(request, "Сохранено" + (" — автоотклики включены" if enabled else ""))
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
    return _back()


@router.post("/plan")
def plan_now(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    added = autoapply_svc.plan(s, user.id)
    flash(request, f"В очередь добавлено: {len(added)}" if added else
          "Новых подходящих вакансий нет (или дневной лимит уже занят)")
    return _back()


@router.post("/pause")
def pause(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    autoapply_svc.pause(s, user.id, "поставлено на паузу вручную")
    autoapply_svc.mark_pause_notified(s, user.id)
    flash(request, "Автоотклики на паузе")
    return _back()


@router.post("/resume")
def resume(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    autoapply_svc.resume_after_pause(s, user.id)
    flash(request, "Продолжаем")
    return _back()


@router.post("/{app_id}/approve")
def approve(app_id: int, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    autoapply_svc.approve(s, user.id, app_id)
    return _back()


@router.post("/{app_id}/cancel")
def cancel(app_id: int, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    autoapply_svc.cancel(s, user.id, app_id)
    return _back()


@router.post("/session")
async def upload_session(request: Request, file: UploadFile = File(...),
                         user: CurrentUser = Depends(current_user)):
    data = await file.read(600 * 1024)
    try:
        autoapply_svc.save_site_session(user.id, data)
        flash(request, "Сессия hh.ru сохранена")
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
    return _back()


@router.post("/session/delete")
def delete_session(request: Request, user: CurrentUser = Depends(current_user)):
    autoapply_svc.delete_site_session(user.id)
    flash(request, "Сессия hh.ru удалена с сервера")
    return _back()
