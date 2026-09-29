from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.bot.api import get_api
from app.core.config import get_settings
from app.core.security import hash_password, verify_password
from app.models import User
from app.services import resumes as resume_svc
from app.services import telegram_links
from app.web.deps import CurrentUser, current_user, db
from app.web.templating import flash, render

router = APIRouter(prefix="/settings")


@router.get("")
def page(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    me = s.get(User, user.id)
    tg = get_settings().telegram
    api = get_api()
    return render(
        request, "settings.html", me=me, resumes=resume_svc.list_for_user(s, user.id),
        bot_enabled=tg.active, bot_username=api.username if api else None,
        default_threshold=tg.notify_min_score,
        link_code=request.session.pop("tg_code", None),
    )


@router.post("/telegram/link")
def telegram_link(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    request.session["tg_code"] = telegram_links.create_code(s, user.id)
    return RedirectResponse("/settings", status_code=303)


@router.post("/telegram/unlink")
def telegram_unlink(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    telegram_links.unlink(s, user.id)
    flash(request, "Telegram отвязан")
    return RedirectResponse("/settings", status_code=303)


@router.post("/preferences")
def preferences(request: Request, notify_min_score: str = Form(""), default_resume_id: str = Form(""),
                notify_off: bool = Form(False), user: CurrentUser = Depends(current_user),
                s: Session = Depends(db)):
    me = s.get(User, user.id)
    value = notify_min_score.strip()
    if notify_off:
        me.notify_min_score = 101  # above any score: notifications off
    else:
        me.notify_min_score = int(value) if value.isdigit() and int(value) <= 100 else None
    if default_resume_id.isdigit():
        me.default_resume_id = resume_svc.get_owned(s, user.id, int(default_resume_id)).id
    else:
        me.default_resume_id = None
    flash(request, "Сохранено")
    return RedirectResponse("/settings", status_code=303)


@router.post("/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...),
                    user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    me = s.get(User, user.id)
    if not verify_password(current, me.password_hash):
        flash(request, "Текущий пароль неверен", "error")
    elif len(new) < 8:
        flash(request, "Новый пароль — минимум 8 символов", "error")
    else:
        me.password_hash = hash_password(new)
        flash(request, "Пароль изменён")
    return RedirectResponse("/settings", status_code=303)
