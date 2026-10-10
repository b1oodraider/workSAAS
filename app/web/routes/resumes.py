from __future__ import annotations

import re

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.features import features_for
from app.models import Analysis, SavedSearch
from app.services import criteria as criteria_svc
from app.services import resumes as resume_svc
from app.services.errors import ValidationFailed
from app.web.deps import CurrentUser, current_user, db, form_fields
from app.web.templating import flash, render

router = APIRouter(prefix="/resumes")


@router.get("")
def list_page(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db, scope="function")):
    return render(request, "resumes/list.html", resumes=resume_svc.list_for_user(s, user.id))


@router.post("")
async def create(
    request: Request,
    title: str = Form(""),
    text: str = Form(""),
    preferences: str = Form(""),
    file: UploadFile | None = File(None),
    user: CurrentUser = Depends(current_user),
    s: Session = Depends(db, scope="function"),
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
        if data:
            # PDF/DOCX parsing is CPU-bound: keep it off the event loop (the job worker shares it).
            text = await run_in_threadpool(resume_svc.text_from_file, filename, data)
        resume = resume_svc.create(s, user.id, title=title, text=text, preferences=preferences,
                                   filename=filename)
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/resumes", status_code=303)
    return RedirectResponse(f"/resumes/{resume.id}", status_code=303)


@router.get("/{resume_id}")
def detail(resume_id: int, request: Request, user: CurrentUser = Depends(current_user),
           s: Session = Depends(db, scope="function")):
    resume = resume_svc.get_owned(s, user.id, resume_id)
    analyses = list(s.scalars(
        select(Analysis).where(Analysis.user_id == user.id, Analysis.resume_id == resume.id,
                               Analysis.vacancy_id.is_(None))
        .order_by(Analysis.id.desc())
    ))
    searches = list(s.scalars(select(SavedSearch).where(SavedSearch.resume_id == resume.id)))
    crit = criteria_svc.load(resume.criteria)
    return render(
        request, "resumes/detail.html", resume=resume, analyses=analyses, searches=searches,
        features=[(f, form_fields(f.params_model)) for f in features_for("resume")],
        crit=crit, crit_missing=criteria_svc.missing(crit), crit_labels=criteria_svc.LABELS,
    )


MULTI = ("work_formats", "employment", "contracts", "grades")
_MONEY_JUNK = re.compile(r"[\s.,_'₽$€£₸]|руб\.?|р\.", re.I)


def _money_input(raw: str) -> int | None:
    """"250 000 ₽", "250000", "250к" -> 250000; nonsense -> a form error, not a silent None."""
    text = _MONEY_JUNK.sub("", raw.strip().lower())
    if not text:
        return None
    thousands = text[-1] in ("k", "к")
    if thousands:
        text = text[:-1]
    if not (text.isascii() and text.isdigit()) or len(text) > 9:
        raise ValidationFailed("Зарплату укажите числом, например 250000")
    return int(text) * (1000 if thousands else 1)
FLAGS = ("salary_net", "stretch_up", "adjacent_roles")


@router.post("/{resume_id}/criteria")
async def save_criteria(resume_id: int, request: Request, user: CurrentUser = Depends(current_user),
                        s: Session = Depends(db, scope="function")):
    form = await request.form()
    data: dict = {k: str(v) for k, v in form.items() if k not in MULTI and k not in FLAGS}
    for key in MULTI:
        data[key] = form.getlist(key)
    for key in FLAGS:
        data[key] = form.get(key) == "on"
    try:
        for key in ("salary_min", "salary_desired"):
            data[key] = _money_input(data.get(key, ""))
        criteria_svc.save(s, user.id, resume_id, data)
        flash(request, "Условия поиска сохранены — учтутся при следующем подборе")
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/resumes/{resume_id}#criteria", status_code=303)


@router.post("/{resume_id}/edit")
def edit(resume_id: int, request: Request, title: str = Form(""), text: str = Form(...),
         preferences: str = Form(""), user: CurrentUser = Depends(current_user),
         s: Session = Depends(db, scope="function")):
    try:
        resume_svc.update(s, user.id, resume_id, title=title, text=text, preferences=preferences)
        flash(request, "Сохранено")
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/resumes/{resume_id}", status_code=303)


@router.post("/{resume_id}/delete")
def delete(resume_id: int, request: Request, user: CurrentUser = Depends(current_user),
           s: Session = Depends(db, scope="function")):
    resume_svc.delete(s, user.id, resume_id)
    flash(request, "Резюме удалено")
    return RedirectResponse("/resumes", status_code=303)
