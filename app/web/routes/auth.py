from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.models import User
from app.web.deps import db
from app.web.templating import flash, render

router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html", user=None)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          s: Session = Depends(db)):
    user = s.scalar(select(User).where(User.username == username.strip()))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        flash(request, "Неверный логин или пароль", "error")
        return RedirectResponse("/login", status_code=303)
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
