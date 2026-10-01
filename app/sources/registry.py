"""Source registry: name -> class; instances are built from config.

To add a source: create app/sources/<name>.py with a JobSource subclass and add it
to SOURCE_CLASSES below. Options go to [sources.<name>] in config.toml.
"""

from __future__ import annotations

from app.core.config import SourceConfig, get_settings
from app.sources.base import JobSource, SourceError
from app.sources.foreign import ArbeitnowSource, HimalayasSource, JobicySource
from app.sources.habr import HabrSource
from app.sources.hh import HHSource
from app.sources.manual import ManualSource
from app.sources.remoteok import RemoteOKSource
from app.sources.remotive import RemotiveSource
from app.sources.rss import RSSSource
from app.sources.superjob import SuperJobSource
from app.sources.telegram_channels import TelegramChannelsSource
from app.sources.trudvsem import TrudvsemSource

SOURCE_CLASSES: dict[str, type[JobSource]] = {
    cls.name: cls
    for cls in (
        HHSource,
        HabrSource,
        SuperJobSource,
        TrudvsemSource,
        TelegramChannelsSource,
        RSSSource,
        RemotiveSource,
        RemoteOKSource,
        HimalayasSource,
        JobicySource,
        ArbeitnowSource,
        ManualSource,
    )
}


def source_config(name: str) -> SourceConfig:
    cls = SOURCE_CLASSES.get(name)
    cfg = get_settings().sources.get(name)
    if cfg is None:
        cfg = SourceConfig(enabled=cls.enabled_by_default if cls else False)
    return cfg


def get_source(name: str) -> JobSource:
    cls = SOURCE_CLASSES.get(name)
    if cls is None:
        raise SourceError(f"Неизвестный источник: {name}")
    cfg = source_config(name)
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
