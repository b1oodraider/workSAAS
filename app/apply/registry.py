"""Which applier handles vacancies of which source. Tests may replace entries.

New site: an `Applier` subclass in `app/apply/<site>.py` and one line in APPLIER_CLASSES.
"""

from __future__ import annotations

from app.apply.base import Applier
from app.apply.hh_browser import HHBrowserApplier

APPLIER_CLASSES: tuple[type[Applier], ...] = (HHBrowserApplier,)
APPLIERS: dict[str, Applier] = {cls.source: cls() for cls in APPLIER_CLASSES}


def applier_for(source: str) -> Applier | None:
    return APPLIERS.get(source)


def supported_sources() -> list[str]:
    return list(APPLIERS)
