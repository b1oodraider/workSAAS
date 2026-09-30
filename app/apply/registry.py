"""Which applier handles vacancies of which source. Tests may replace entries."""

from __future__ import annotations

from app.apply.base import Applier
from app.apply.hh_browser import HHBrowserApplier

APPLIERS: dict[str, Applier] = {"hh": HHBrowserApplier()}


def applier_for(source: str) -> Applier | None:
    return APPLIERS.get(source)


def supported_sources() -> list[str]:
    return list(APPLIERS)
