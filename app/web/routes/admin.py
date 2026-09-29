from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.services import admin as admin_svc
from app.services.errors import ValidationFailed
from app.web.deps import CurrentUser, current_user, db
from app.web.templating import flash, render

router = APIRouter(prefix="/admin")


def require_admin(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Только для администратора")
    return user


def _budget(value: str) -> float | None:
    value = value.strip().replace(",", ".")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        raise ValidationFailed("Бюджет — число в долларах") from None


@router.get("")
def page(request: Request, admin: CurrentUser = Depends(require_admin), s: Session = Depends(db)):
    sources, runs = admin_svc.sources_health(s)
    return render(request, "admin.html", users=admin_svc.users_overview(s), sources=sources, runs=runs,
                  llm=admin_svc.llm_health(s), new_password=request.session.pop("new_password", None))


@router.post("/users")
def create_user(request: Request, username: str = Form(...), password: str = Form(""),
                budget: str = Form(""), is_admin: bool = Form(False),
                admin: CurrentUser = Depends(require_admin), s: Session = Depends(db)):
    try:
        user, pw = admin_svc.create_user(s, username, password=password, is_admin=is_admin, budget=_budget(budget))
        # Shown once on the next page load; never logged.
        request.session["new_password"] = {"username": user.username, "password": pw}
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/admin", status_code=303)


@router.post("/users/{user_id}")
def update_user(user_id: int, request: Request, action: str = Form(...), budget: str = Form(""),
                admin: CurrentUser = Depends(require_admin), s: Session = Depends(db)):
    try:
        if action == "budget":
            admin_svc.set_budget(s, user_id, _budget(budget))
            flash(request, "Бюджет обновлён")
        elif action in ("block", "unblock"):
            admin_svc.set_active(s, admin.id, user_id, action == "unblock")
            flash(request, "Готово")
        elif action == "reset_password":
            pw = admin_svc.reset_password(s, user_id)
            from app.models import User

            request.session["new_password"] = {"username": s.get(User, user_id).username, "password": pw}
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/admin", status_code=303)
