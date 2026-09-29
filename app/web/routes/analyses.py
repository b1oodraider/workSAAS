from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.features import FEATURES, get_feature
from app.models import Resume, Vacancy
from app.services import analysis as analysis_svc
from app.services.errors import ValidationFailed
from app.web.deps import CurrentUser, current_user, db, params_from_form, safe_path
from app.web.templating import flash, render

router = APIRouter(prefix="/analyses")


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.post("")
async def start(request: Request, user: CurrentUser = Depends(current_user)):
    form = await request.form()
    kind = str(form.get("kind", ""))
    back = safe_path(str(form.get("back", "/")))
    if kind not in FEATURES:
        flash(request, "Неизвестная функция", "error")
        return RedirectResponse(back, status_code=303)
    feature = get_feature(kind)
    try:
        job_id = analysis_svc.enqueue_analysis(
            user.id, kind,
            resume_id=_int_or_none(form.get("resume_id")),
            vacancy_id=_int_or_none(form.get("vacancy_id")),
            params=params_from_form(feature.params_model, form),
        )
    except ValidationFailed as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(back, status_code=303)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.get("/{analysis_id}")
def detail(analysis_id: int, request: Request, user: CurrentUser = Depends(current_user),
           s: Session = Depends(db)):
    a = analysis_svc.get_owned(s, user.id, analysis_id)
    return render(
        request, "analyses/detail.html", a=a, feature=FEATURES.get(a.kind),
        resume=s.get(Resume, a.resume_id) if a.resume_id else None,
        vacancy=s.get(Vacancy, a.vacancy_id) if a.vacancy_id else None,
    )
