from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.features import features_for
from app.models import Analysis, UserVacancy, UserVacancyStatus, Vacancy
from app.services import resumes as resume_svc
from app.services import vacancies as vacancy_svc
from app.services.errors import ValidationFailed
from app.web.deps import CurrentUser, current_user, db, form_fields, safe_path
from app.web.templating import flash, render

router = APIRouter(prefix="/vacancies")

STATUS_LABELS = {
    "new": "новая",
    "saved": "в избранном",
    "applied": "откликнулся",
    "interview": "собеседование",
    "rejected": "отказ",
    "hidden": "скрыта",
}


@router.get("")
def list_page(request: Request, status: str = "", q: str = "",
              user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    query = select(UserVacancy).join(Vacancy).where(UserVacancy.user_id == user.id)
    if status:
        query = query.where(UserVacancy.status == status)
    else:
        query = query.where(UserVacancy.status != UserVacancyStatus.hidden)
    if q:
        like = f"%{q}%"
        query = query.where(or_(Vacancy.title.ilike(like), Vacancy.company.ilike(like)))
    rows = list(s.scalars(query.order_by(UserVacancy.id.desc()).limit(300)))
    return render(request, "vacancies/list.html", rows=rows, status=status, q=q,
                  statuses=STATUS_LABELS)


@router.post("")
def create(request: Request, mode: str = Form("text"), title: str = Form(""),
           company: str = Form(""), url: str = Form(""), text: str = Form(""),
           user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    try:
        if mode == "url":
            job_id = vacancy_svc.enqueue_import(user.id, url.strip())
            return RedirectResponse(f"/jobs/{job_id}", status_code=303)
        vacancy = vacancy_svc.create_manual(s, user.id, title=title, text=text, url=url,
                                            company=company)
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/vacancies", status_code=303)
    return RedirectResponse(f"/vacancies/{vacancy.id}", status_code=303)


@router.get("/{vacancy_id}")
def detail(vacancy_id: int, request: Request, user: CurrentUser = Depends(current_user),
           s: Session = Depends(db)):
    vacancy, uv = vacancy_svc.get_for_user(s, user.id, vacancy_id)
    analyses = list(s.scalars(
        select(Analysis).where(Analysis.user_id == user.id, Analysis.vacancy_id == vacancy.id)
        .order_by(Analysis.id.desc())
    ))
    return render(
        request, "vacancies/detail.html", vacancy=vacancy, uv=uv, analyses=analyses,
        resumes=resume_svc.list_for_user(s, user.id), statuses=STATUS_LABELS,
        vacancy_features=[(f, form_fields(f.params_model)) for f in features_for("vacancy")],
        pair_features=[(f, form_fields(f.params_model)) for f in features_for("resume_vacancy")],
    )


@router.post("/{vacancy_id}/status")
def set_status(vacancy_id: int, request: Request, status: str = Form(...),
               notes: str | None = Form(None), back: str = Form(""),
               user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    try:
        vacancy_svc.set_status(s, user.id, vacancy_id, status, notes)
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(safe_path(back, f"/vacancies/{vacancy_id}"), status_code=303)
