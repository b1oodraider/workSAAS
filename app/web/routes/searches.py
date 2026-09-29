from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import SavedSearch
from app.services import resumes as resume_svc
from app.services import search as search_svc
from app.services.errors import ValidationFailed
from app.sources import available_sources
from app.web.deps import CurrentUser, current_user, db
from app.web.routes.vacancies import STATUS_LABELS
from app.web.templating import flash, render

router = APIRouter(prefix="/searches")

EXPERIENCE = {
    "": "любой",
    "noExperience": "без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "более 6 лет",
}


AREAS = {"": "как в настройках", "113": "Россия", "1": "Москва", "2": "Санкт-Петербург",
         "4": "Новосибирск", "3": "Екатеринбург", "88": "Казань", "16": "Беларусь", "40": "Казахстан"}


def _list(request: Request, user: CurrentUser, s: Session, form=None):
    searches = list(s.scalars(
        select(SavedSearch).where(SavedSearch.user_id == user.id).order_by(SavedSearch.id.desc())
    ))
    values = {k: form.get(k) for k in form.keys()} if form is not None else {}
    checked = form.getlist("sources") if form is not None else None
    return render(request, "searches/list.html", searches=searches,
                  resumes=resume_svc.list_for_user(s, user.id),
                  sources=available_sources(searchable_only=True), experience=EXPERIENCE, areas=AREAS,
                  preselect=values.get("resume_id") or request.query_params.get("resume_id", ""),
                  values=values, checked=checked, top_n=get_settings().matching.top_n)


@router.get("")
def list_page(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    return _list(request, user, s)


@router.post("")
async def create(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    form = await request.form()
    try:
        salary = str(form.get("salary_min", "")).strip()
        search = search_svc.create(
            s, user.id,
            resume_id=int(form.get("resume_id") or 0),
            name=str(form.get("name", "")),
            sources=[str(x) for x in form.getlist("sources")],
            queries=str(form.get("queries", "")).splitlines(),
            filters={
                "area": str(form.get("area", "")).strip(),
                "salary_min": int(salary) if salary.isdigit() else None,
                "remote_only": "remote_only" in form,
                "experience": str(form.get("experience", "")),
                "period_days": int(form.get("period_days") or 14),
                "exclude_words": [w.strip() for w in str(form.get("exclude_words", "")).split(",")
                                  if w.strip()],
            },
            interval_minutes=int(form.get("interval_minutes") or 0),
        )
    except (ValidationFailed, ValueError) as exc:
        flash(request, str(exc) if isinstance(exc, ValidationFailed) else "Проверьте числа в форме", "error")
        return _list(request, user, s, form=form)
    s.commit()
    job_id = search_svc.enqueue_search_run(search.id, user.id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.get("/{search_id}")
def detail(search_id: int, request: Request, hidden: int = 0,
           user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    search = search_svc.get_owned(s, user.id, search_id)
    items = search_svc.results(s, user.id, search_id, include_hidden=bool(hidden))
    resume = resume_svc.get_owned(s, user.id, search.resume_id)
    return render(request, "searches/detail.html", search=search, items=items, resume=resume,
                  statuses=STATUS_LABELS, experience=EXPERIENCE, hidden=hidden)


@router.post("/{search_id}/run")
def run(search_id: int, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    search_svc.get_owned(s, user.id, search_id)
    job_id = search_svc.enqueue_search_run(search_id, user.id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/{search_id}/delete")
def delete(search_id: int, request: Request, user: CurrentUser = Depends(current_user),
           s: Session = Depends(db)):
    s.delete(search_svc.get_owned(s, user.id, search_id))
    flash(request, "Поиск удалён")
    return RedirectResponse("/searches", status_code=303)
