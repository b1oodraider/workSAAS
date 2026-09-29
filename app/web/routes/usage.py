from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.llm import get_gateway
from app.models import LLMUsage, User
from app.web.deps import CurrentUser, current_user, db
from app.web.templating import render

router = APIRouter(prefix="/usage")


@router.get("")
def usage_page(request: Request, user: CurrentUser = Depends(current_user), s: Session = Depends(db)):
    now = utcnow()
    month_start = datetime(now.year, now.month, 1)
    cols = (
        LLMUsage.task,
        LLMUsage.model,
        func.count(),
        func.sum(case((LLMUsage.cached.is_(True), 1), else_=0)),
        func.sum(LLMUsage.input_tokens),
        func.sum(LLMUsage.output_tokens),
        func.sum(LLMUsage.cost_usd),
    )
    mine = s.execute(
        select(*cols).where(LLMUsage.user_id == user.id, LLMUsage.created_at >= month_start)
        .group_by(LLMUsage.task, LLMUsage.model)
    ).all()
    gw = get_gateway()
    everyone = []
    if user.is_admin:
        everyone = s.execute(
            select(User.username, func.count(LLMUsage.id), func.sum(LLMUsage.cost_usd))
            .join(LLMUsage, LLMUsage.user_id == User.id)
            .where(LLMUsage.created_at >= month_start)
            .group_by(User.username)
        ).all()
    return render(request, "usage.html", rows=mine, spent=gw.month_spent(user.id),
                  budget=gw.budget_for(user.id), everyone=everyone)
