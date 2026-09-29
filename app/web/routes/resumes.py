from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.features import features_for
from app.models import Analysis, SavedSearch
from app.services import resumes as resume_svc
from app.services.errors import ValidationFailed
from app.web.deps import CurrentUser, current_user, db, form_fields
from app.web.templating import flash, render

router = APIRouter(prefix="/resumes")


@router.get("")
def list_page(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    return render(request, "resumes/list.html", resumes=resume_svc.list_for_user(s, user.id))


@router.post("")
async def create(
    request: Request,
    title: str = Form(""),
    text: str = Form(""),
    preferences: str = Form(""),
    file: UploadFile | None = File(None),
    user: CurrentUser = Depends(current_user),
    s: Session = Depends(db),
):
    data = filename = None
    if file is not None and file.filename:
        limit = get_settings().max_upload_mb * 1024 * 1024
        data = await file.read(limit + 1)
        if len(data) > limit:
            flash(request, f"Файл больше {get_settings().max_upload_mb} МБ", "error")
            return RedirectResponse("/resumes", status_code=303)
        filename = file.filename
    try:
        resume = resume_svc.create(s, user.id, title=title, text=text, preferences=preferences,
                                   filename=filename, file_data=data)
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/resumes", status_code=303)
    return RedirectResponse(f"/resumes/{resume.id}", status_code=303)


@router.get("/{resume_id}")
def detail(resume_id: int, request: Request, user: CurrentUser = Depends(current_user),
           s: Session = Depends(db)):
    resume = resume_svc.get_owned(s, user.id, resume_id)
    analyses = list(s.scalars(
        select(Analysis).where(Analysis.user_id == user.id, Analysis.resume_id == resume.id,
                               Analysis.vacancy_id.is_(None))
        .order_by(Analysis.id.desc())
    ))
    searches = list(s.scalars(select(SavedSearch).where(SavedSearch.resume_id == resume.id)))
    return render(
        request, "resumes/detail.html", resume=resume, analyses=analyses, searches=searches,
        features=[(f, form_fields(f.params_model)) for f in features_for("resume")],
    )


@router.post("/{resume_id}/edit")
def edit(resume_id: int, request: Request, title: str = Form(""), text: str = Form(...),
         preferences: str = Form(""), user: CurrentUser = Depends(current_user),
         s: Session = Depends(db)):
    try:
        resume_svc.update(s, user.id, resume_id, title=title, text=text, preferences=preferences)
        flash(request, "Сохранено")
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/resumes/{resume_id}", status_code=303)


@router.post("/{resume_id}/delete")
def delete(resume_id: int, request: Request, user: CurrentUser = Depends(current_user),
           s: Session = Depends(db)):
    resume_svc.delete(s, user.id, resume_id)
    flash(request, "Резюме удалено")
    return RedirectResponse("/resumes", status_code=303)
