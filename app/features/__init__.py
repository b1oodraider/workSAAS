"""Feature registry. To add a feature: create app/features/<name>/ and import it below."""

from __future__ import annotations

from app.features.base import AnalysisFeature, NoParams, Subject

FEATURES: dict[str, AnalysisFeature] = {}


def register(feature: AnalysisFeature) -> AnalysisFeature:
    if feature.kind in FEATURES:
        raise RuntimeError(f"Feature {feature.kind!r} registered twice")
    FEATURES[feature.kind] = feature
    return feature


def get_feature(kind: str) -> AnalysisFeature:
    try:
        return FEATURES[kind]
    except KeyError:
        raise KeyError(f"Unknown feature: {kind}") from None


def features_for(subject: Subject, *, include_hidden: bool = False) -> list[AnalysisFeature]:
    return sorted(
        (f for f in FEATURES.values() if f.subject == subject and (include_hidden or not f.hidden)),
        key=lambda f: f.order,
    )


# --- feature modules (each calls register() on import) ---------------------
from app.features import (  # noqa: E402,F401
    cover_letter,
    follow_up,
    interview_prep,
    letter_critic,
    match,
    offer_negotiation,
    recruiter_reply,
    resume_profile,
    resume_review,
    tailor_resume,
    vacancy_review,
)

__all__ = ["FEATURES", "AnalysisFeature", "NoParams", "features_for", "get_feature", "register"]
