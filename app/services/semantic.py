"""Meaning similarity of vacancies to a resume ([llm.embeddings]) — for the search prefilter and
anything else that ranks vacancies for a candidate."""

from __future__ import annotations

import logging

from app.core.config import get_settings
from app.features.resume_profile.schema import ResumeProfile
from app.llm import BudgetExceeded, LLMError, get_gateway
from app.models import Vacancy
from app.ranking.semantic import cosine, similarity_score
from app.services import criteria as criteria_svc

log = logging.getLogger(__name__)


def resume_query(profile: ResumeProfile, crit: criteria_svc.SearchCriteria, resume_text: str) -> str:
    """What vacancies are compared with by meaning: who the candidate is, then the resume itself."""
    roles = [*profile.roles, *crit.roles_extra, *(profile.adjacent_roles if crit.adjacent_roles else [])]
    return "\n".join([profile.headline, "Должности: " + ", ".join(roles),
                      "Навыки: " + ", ".join(sk.name for sk in profile.skills), "", resume_text])


def vacancy_text(vacancy: Vacancy) -> str:
    return f"{vacancy.title}\n{', '.join(vacancy.skills or [])}\n{vacancy.description}"


def enabled() -> bool:
    return get_settings().llm.embeddings.enabled


async def similarities(user_id: int, query: str, items: list[tuple[int, str]],
                       errors: list[str]) -> dict[int, float]:
    """vacancy id -> meaning similarity to ``query`` (0..100), for at most ``max_per_run`` items
    (pass them best first). Empty when off or unavailable — the caller falls back to keywords;
    a text the provider rejects is simply left out."""
    cfg = get_settings().llm.embeddings
    if not cfg.enabled or not items:
        return {}
    items = items[: cfg.max_per_run]
    try:
        vecs = await get_gateway().embed([query, *(text for _, text in items)], user_id=user_id, partial=True)
    except BudgetExceeded as exc:
        errors.append(f"Отбор по смыслу пропущен: {exc}")
        return {}
    except LLMError as exc:
        # Provider error bodies may carry account details of the operator's key: log, don't show.
        log.warning("embeddings failed: %s", exc)
        errors.append("Отбор по смыслу недоступен (ошибка сервиса эмбеддингов) — отобрано только по словам")
        return {}
    resume_vec = vecs[0]
    if resume_vec is None:
        errors.append("Отбор по смыслу пропущен: сервис эмбеддингов не принял текст резюме")
        return {}
    return {vid: similarity_score(cosine(resume_vec, vec), cfg.low, cfg.high)
            for (vid, _), vec in zip(items, vecs[1:])
            if vec is not None and len(vec) == len(resume_vec)}  # another model's vectors: skip

