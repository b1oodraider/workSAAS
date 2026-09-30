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
APP_STATUS_LABELS = {"queued": "в очереди", "review": "проверьте письмо", "approved": "подтверждён, ждёт отправки",
                     "sending": "отправляется", "applied": "отправлен", "skipped": "пропущен автоматически",
                     "failed": "не удалось", "cancelled": "вы отказались"}
MAX_UPLOAD = 600 * 1024


def _back(anchor: str = "") -> RedirectResponse:
    return RedirectResponse("/autoapply" + anchor, status_code=303)


@router.get("")
def page(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    return render(request, "autoapply.html", data=autoapply_svc.overview(s, user.id), modes=MODE_LABELS,
                  app_status=APP_STATUS_LABELS)


@router.post("/settings")
def save_settings(request: Request, enabled: bool = Form(False), mode: str = Form("confirm"),
                  min_score: int = Form(80), daily_limit: int = Form(10), min_interval_min: int = Form(2),
                  active_from_hour: int = Form(9), active_to_hour: int = Form(21), resume_id: str = Form(""),
                  site_resume_title: str = Form(""), letter_tone: str = Form("friendly"),
                  user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    try:
        cfg = autoapply_svc.update_config(
            s, user.id, enabled=enabled, mode=mode, min_score=min_score, daily_limit=daily_limit,
            min_interval_s=min_interval_min * 60, active_from_hour=active_from_hour,
            active_to_hour=active_to_hour, resume_id=int(resume_id) if resume_id.isdigit() else None,
            site_resume_title=site_resume_title, letter_tone=letter_tone)
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
        return _back()
    idle = autoapply_svc.idle_reason(s, user.id)
    if not enabled:
        flash(request, "Сохранено. Автоотклики выключены")
    elif idle and not cfg.paused_reason:
        flash(request, f"Сохранено, но отклики пока не пойдут: {idle}", "error")
    elif cfg.paused_reason:
        flash(request, "Сохранено. Автоотклики на паузе — нажмите «Продолжить», когда будете готовы")
    else:
        flash(request, "Сохранено — автоотклики работают")
    return _back()


@router.post("/plan")
def plan_now(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    added = autoapply_svc.plan(s, user.id)
    cfg = autoapply_svc.get_config(s, user.id)
    if added:
        note = "" if cfg.enabled else " (автоотклики выключены — включите их в настройках)"
        flash(request, f"В очередь добавлено: {len(added)}. Письма напишу в течение нескольких минут{note}")
    else:
        flash(request, f"Новых вакансий с оценкой от {cfg.min_score} нет, или очередь уже заполнена на сегодня. "
                       "Запустите подбор в «Поисках» или снизьте порог")
    return _back()


@router.post("/pause")
def pause(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    autoapply_svc.pause(s, user.id, "вы поставили паузу")
    autoapply_svc.mark_pause_notified(s, user.id)
    flash(request, "Автоотклики на паузе")
    return _back()


@router.post("/resume")
def resume(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    autoapply_svc.resume_after_pause(s, user.id)
    idle = autoapply_svc.idle_reason(s, user.id)
    flash(request, "Автоотклики снова работают" + (f", но пока ждут: {idle}" if idle else ""))
    return _back()


@router.post("/{app_id}/approve")
def approve(request: Request, app_id: int, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    flash(request, autoapply_svc.approve(s, user.id, app_id))
    return _back("#queue")


@router.post("/{app_id}/cancel")
def cancel(request: Request, app_id: int, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    flash(request, autoapply_svc.cancel(s, user.id, app_id))
    return _back("#queue")


@router.post("/{app_id}/restore")
def restore(request: Request, app_id: int, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    flash(request, autoapply_svc.restore(s, user.id, app_id))
    return _back("#queue")


@router.post("/session/{site}")
def upload_session(request: Request, site: str, file: UploadFile = File(...),
                   user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    data = file.file.read(MAX_UPLOAD + 1)
    try:
        resumed = autoapply_svc.save_site_session(s, user.id, site, data)
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
        return _back(f"#login-{site}")
    flash(request, "Вход сохранён" + (" — автоотклики продолжены" if resumed else ""))
    return _back()


@router.post("/session/{site}/delete")
def delete_session(request: Request, site: str, user: CurrentUser = Depends(current_user)):
    autoapply_svc.delete_site_session(user.id, site)
    flash(request, "Вход удалён с сервера. Автоотклики не пойдут, пока вы не войдёте снова")
    return _back()
