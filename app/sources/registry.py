"""Source registry: name -> class; instances are built from config."""

from __future__ import annotations

from app.core.config import SourceConfig, get_settings
from app.sources.base import JobSource, SourceError
from app.sources.hh import HHSource
from app.sources.manual import ManualSource
from app.sources.rss import RSSSource

SOURCE_CLASSES: dict[str, type[JobSource]] = {
    cls.name: cls for cls in (HHSource, RSSSource, ManualSource)
}


def get_source(name: str) -> JobSource:
    cls = SOURCE_CLASSES.get(name)
    if cls is None:
        raise SourceError(f"Неизвестный источник: {name}")
    cfg = get_settings().sources.get(name) or SourceConfig()
    if not cfg.enabled:
        raise SourceError(f"Источник {name} выключен в конфиге")
    return cls(cfg)


def available_sources(*, searchable_only: bool = False) -> list[JobSource]:
    result = []
    for name in SOURCE_CLASSES:
        try:
            src = get_source(name)
        except SourceError:
            continue
        if searchable_only and not src.searchable:
            continue
        result.append(src)
    return result
