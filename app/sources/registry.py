"""Source registry: name -> class; instances are built from config.

To add a source: create app/sources/<name>.py with a JobSource subclass and add it
to SOURCE_CLASSES below. Options go to [sources.<name>] in config.toml.
"""

from __future__ import annotations

from app.core.config import SourceConfig, get_settings
from app.sources.adzuna import AdzunaSource
from app.sources.arbeitnow import ArbeitnowSource
from app.sources.ashby import AshbySource
from app.sources.avito_jobs import AvitoJobsSource
from app.sources.base import JobSource, SourceError
from app.sources.djinni import DjinniSource
from app.sources.geekjob import GeekjobSource
from app.sources.getmatch import GetmatchSource
from app.sources.greenhouse import GreenhouseSource
from app.sources.habr import HabrSource
from app.sources.hh import HHSource
from app.sources.himalayas import HimalayasSource
from app.sources.hn_hiring import HNHiringSource
from app.sources.jobicy import JobicySource
from app.sources.lever import LeverSource
from app.sources.linkedin import LinkedInSource
from app.sources.manual import ManualSource
from app.sources.rabota_ru import RabotaRuSource
from app.sources.remote_job_ru import RemoteJobRuSource
from app.sources.remoteok import RemoteOKSource
from app.sources.remotive import RemotiveSource
from app.sources.rss import RSSSource
from app.sources.superjob import SuperJobSource
from app.sources.telegram_channels import TelegramChannelsSource
from app.sources.themuse import TheMuseSource
from app.sources.trudvsem import TrudvsemSource
from app.sources.weworkremotely import WeWorkRemotelySource
from app.sources.workingnomads import WorkingNomadsSource
from app.sources.yandex_jobs import YandexJobsSource

SOURCE_CLASSES: dict[str, type[JobSource]] = {
    cls.name: cls
    for cls in (
        # Russia / CIS
        HHSource,
        HabrSource,
        SuperJobSource,
        TrudvsemSource,
        RabotaRuSource,
        GeekjobSource,
        YandexJobsSource,
        GetmatchSource,
        RemoteJobRuSource,
        DjinniSource,
        AvitoJobsSource,
        TelegramChannelsSource,
        # International
        RemotiveSource,
        RemoteOKSource,
        HimalayasSource,
        JobicySource,
        WeWorkRemotelySource,
        WorkingNomadsSource,
        ArbeitnowSource,
        TheMuseSource,
        AdzunaSource,
        LinkedInSource,
        HNHiringSource,
        # Employers' own career pages (ATS boards)
        GreenhouseSource,
        LeverSource,
        AshbySource,
        # Feeds and manual input
        RSSSource,
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
