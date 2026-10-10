"""Public Telegram channels with job posts, read via the web preview https://t.me/s/<channel>.

No Telegram account or bot is needed; only public channels work. Most channels mix
vacancies with ads, resumes and news, so posts go through a cheap rule-based
classifier before they become vacancies.

Options:
  channels      list of channel usernames (without @)
  pages         how many preview pages (~20 posts each) to read per channel (1..5, default 2)
  min_interval  seconds between requests to t.me (default 2, at least 1)
  max_queries   only the first N queries of a search run may fetch channels (default 3);
                channels are read once per run and every query is matched against them
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup, Tag

from app.core.text import html_to_text
from app.sources.base import (
    JobSource,
    SearchQuery,
    SourceError,
    VacancyDraft,
    matches_query,
    query_words,
    relevance_key,
    safe_map,
)
from app.sources.salary import parse_salary_text
from app.sources.jsonld import parse_date
from app.sources.web import make_fetcher

log = logging.getLogger(__name__)

_POSITIVE = (
    "ваканси", "#vacancy", "#job", "#hiring", "ищем ", "в команду", "требования", "обязанности",
    "зарплат", "з/п", "зп:", "вилка", "оклад", "условия", "откликнуться", "отклик", "резюме присылайте",
    "удалённ", "удаленн", "гибрид", "офис", "грейд", "junior", "middle", "senior", "hiring",
    "responsibilities", "requirements", "salary",
)
_NEGATIVE = (
    "#резюме", "#resume", "#cv", "ищу работу", "#ищуработу", "#реклама", "#ad ",
    "розыгрыш", "подписывайтесь", "вебинар", "курс со скидкой",
)
_ERID_RE = re.compile(r"\berid\b")  # ad marking required by Russian law
_HASHTAG_RE = re.compile(r"#[\w\d_]+")
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️]")
_COMPANY_RE = re.compile(r"(?:компания|company|работодатель)\s*[:—-]\s*(.+)", re.I)
_SALARY_LINE_RE = re.compile(r"(₽|руб|\$|€|usd|eur|\d\s?[kк]\b|зп|з/п|зарплат|вилка|оклад)", re.I)
# Public channel usernames go into t.me URL paths.
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")


def is_vacancy_post(text: str) -> bool:
    low = text.lower()
    if len(low) < 120:
        return False
    if any(n in low for n in _NEGATIVE) or _ERID_RE.search(low):
        return False
    return sum(1 for p in _POSITIVE if p in low) >= 3


def _title(text: str) -> str:
    for line in text.splitlines():
        clean = _EMOJI_RE.sub("", _HASHTAG_RE.sub("", line)).strip(" -—:|*•")
        if len(clean) >= 4:
            return clean[:150]
    return "Вакансия из Telegram"


def post_to_draft(channel: str, post_id: str, text: str, published: str | None) -> VacancyDraft:
    company = None
    m = _COMPANY_RE.search(text)
    if m:
        company = m.group(1).strip()[:200]
    salary_line = next((ln for ln in text.splitlines() if _SALARY_LINE_RE.search(ln)), "")
    sal_from, sal_to, currency, gross = parse_salary_text(salary_line)
    low = text.lower()
    return VacancyDraft(
        source="telegram",
        external_id=f"{channel}/{post_id}",
        title=_title(text),
        url=f"https://t.me/{channel}/{post_id}",
        company=company,
        salary_from=sal_from,
        salary_to=sal_to,
        currency=currency,
        salary_gross=gross,
        remote=True if ("удалён" in low or "удален" in low or "remote" in low) else None,
        description=f"Пост из Telegram-канала @{channel}:\n\n{text}",
        published_at=parse_date(published),
    )


def parse_channel_page(html: str, channel: str) -> tuple[list[VacancyDraft], str | None]:
    """Return vacancy drafts and the smallest post id on the page (for pagination)."""
    soup = BeautifulSoup(html, "html.parser")
    drafts: list[VacancyDraft] = []
    min_id: int | None = None
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        data_post = str(msg.get("data-post", ""))
        post_id = data_post.rsplit("/", 1)[-1]
        if post_id.isdigit():
            min_id = int(post_id) if min_id is None else min(min_id, int(post_id))
        body = msg.select_one("div.tgme_widget_message_text")
        if not isinstance(body, Tag) or not post_id.isdigit():
            continue
        text = html_to_text(str(body))
        if not is_vacancy_post(text):
            continue
        time_el = msg.select_one("time[datetime]")
        published = str(time_el["datetime"]) if time_el else None
        drafts += safe_map(lambda t: post_to_draft(channel, post_id, t, published), [text], source="telegram")
    return drafts, (str(min_id) if min_id is not None else None)


# Public channels with IT vacancies in Russian, checked 2026-10-08 (t.me/s/<name> answers and most
# recent posts pass is_vacancy_post). Used when config.toml has no `channels` option.
DEFAULT_CHANNELS = (
    "it_vakansii_jobs", "remocate", "product_jobs", "javadevjob", "pydevjob", "godevjob", "phpdevjob",
    "jsdevjob", "csharpdevjob", "cppdevjob", "datajob", "datasciencejobs", "datajobschannel", "devjobs",
    "gamedevjob", "careerspace", "jc_it", "remote_job_ru", "geekjobs", "jobforjunior",
)


class TelegramChannelsSource(JobSource):
    name = "telegram"
    trusted = False
    checked_by_default = False
    title = "Telegram-каналы"
    hint = "IT-каналы с вакансиями; список можно сменить в конфиге"

    def _channels(self) -> list[str]:
        channels = [c.lstrip("@").strip() for c in self.opt_list("channels", DEFAULT_CHANNELS)]
        return [c for c in channels if _CHANNEL_RE.match(c)]

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        channels = self._channels()
        if not channels:
            return []
        # Channels are read once per run (memoised parsed posts) and filtered for every query;
        # only the first ``max_queries`` queries may reach t.me for channels not read yet.
        allow_network = self.take_query_slot()
        pending = [c for c in channels if allow_network or self.is_memoized(("channel", c))]
        if not pending:
            return []
        pages = self.opt_int("pages", 2, lo=1, hi=5)
        fetcher = make_fetcher("http", use_proxy=self.cfg.use_proxy, cache_ttl=600,
                               min_interval=self.opt_float("min_interval", 2.0, floor=1.0))
        words = query_words(query.text)
        drafts: dict[str, VacancyDraft] = {}
        failed: list[str] = []
        try:
            for channel in pending:
                try:
                    posts = await self.memoized(("channel", channel),
                                                lambda c=channel: self._read_channel(fetcher, c, pages))
                except SourceError as exc:
                    log.warning("telegram channel %s: %s", channel, exc)
                    failed.append(channel)
                    continue
                for d in posts:
                    if matches_query(f"{d.title}\n{d.description}", query.text):
                        drafts.setdefault(d.external_id, d)
        finally:
            await fetcher.close()
        if failed and len(failed) == len(pending):
            raise SourceError("t.me недоступен или ограничил запросы")
        result = sorted(drafts.values(), reverse=True,
                        key=lambda d: relevance_key(d.title, d.description, d.published_at, words))
        return result[:limit]

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        """Single post (used for URL import); no vacancy classifier here — the user chose it."""
        channel, _, post_id = external_id.partition("/")
        if not _CHANNEL_RE.match(channel) or not post_id.isdigit():
            return None
        fetcher = make_fetcher("http", use_proxy=self.cfg.use_proxy)
        try:
            page = await fetcher.get(f"https://t.me/{channel}/{post_id}", {"embed": "1"})
        finally:
            await fetcher.close()
        if page.status >= 400:
            return None
        soup = BeautifulSoup(page.text, "html.parser")
        body = soup.select_one("div.tgme_widget_message_text")
        if not isinstance(body, Tag):
            return None
        time_el = soup.select_one("time[datetime]")
        return post_to_draft(channel, post_id, html_to_text(str(body)),
                             str(time_el["datetime"]) if time_el else None)

    async def _read_channel(self, fetcher, channel: str, pages: int) -> list[VacancyDraft]:
        found: list[VacancyDraft] = []
        before: str | None = None
        for _ in range(pages):
            page = await fetcher.get(f"https://t.me/s/{channel}", {"before": before} if before else None)
            if page.status == 429:
                raise SourceError("t.me ограничил частоту запросов (429)")
            if page.status >= 400:
                break
            items, before = parse_channel_page(page.text, channel)
            found += items
            if not before:
                break
        return found

    def external_id_from_url(self, url: str) -> str | None:
        m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]{4,})/(\d+)", url)
        return f"{m.group(1)}/{m.group(2)}" if m else None
