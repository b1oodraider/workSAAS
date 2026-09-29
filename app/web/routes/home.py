from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Job, JobStatus, Resume, SavedSearch, User
from app.services import matches as matches_svc
from app.services import vacancies as vacancy_svc
from app.web.deps import CurrentUser, current_user, db
from app.web.templating import render

router = APIRouter()


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
    matches = [(v, a) for _, v, a in matches_svc.top_matches(s, user.id, limit=6)]
    return render(request, "home.html", steps=steps, onboarding=not all(st["done"] for st in steps),
                  due=vacancy_svc.tracker(s, user.id)["due"], matches=matches, running=running)
