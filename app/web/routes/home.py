from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Analysis, Job, JobStatus, Resume, SavedSearch, User, UserVacancy, Vacancy
from app.services import vacancies as vacancy_svc
from app.web.deps import CurrentUser, current_user, db
from app.web.templating import render

router = APIRouter()


def top_matches(s: Session, user_id: int, limit: int = 6) -> list[tuple[Vacancy, Analysis]]:
    latest = (
        select(Analysis.vacancy_id, func.max(Analysis.id).label("aid"))
        .where(Analysis.user_id == user_id, Analysis.kind == "match")
        .group_by(Analysis.vacancy_id).subquery()
    )
    return list(s.execute(
        select(Vacancy, Analysis)
        .join(latest, latest.c.vacancy_id == Vacancy.id)
        .join(Analysis, Analysis.id == latest.c.aid)
        .join(UserVacancy, (UserVacancy.vacancy_id == Vacancy.id) & (UserVacancy.user_id == user_id))
        .where(UserVacancy.status.in_(["new", "saved"]))
        .order_by(Analysis.score.desc().nullslast()).limit(limit)
    ).all())


@router.get("/")
def home(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    me = s.get(User, user.id)
    has_resume = s.scalar(select(func.count(Resume.id)).where(Resume.user_id == user.id)) > 0
    has_search = s.scalar(select(func.count(SavedSearch.id)).where(SavedSearch.user_id == user.id)) > 0
    bot_enabled = get_settings().telegram.active
    steps = [
        {"done": has_resume, "text": "Загрузите резюме", "url": "/resumes"},
        {"done": has_search, "text": "Создайте поиск вакансий по резюме", "url": "/searches"},
    ]
    if bot_enabled:
        steps.append({"done": bool(me.telegram_chat_id), "text": "Привяжите Telegram, чтобы получать подходящие вакансии",
                      "url": "/settings"})
    running = list(s.scalars(
        select(Job).where(Job.user_id == user.id, Job.status.in_([JobStatus.queued, JobStatus.running]),
                          Job.parent_id.is_(None))
        .order_by(Job.id.desc()).limit(5)
    ))
    return render(request, "home.html", steps=steps, onboarding=not all(st["done"] for st in steps),
                  due=vacancy_svc.tracker(s, user.id)["due"], matches=top_matches(s, user.id),
                  running=running)
