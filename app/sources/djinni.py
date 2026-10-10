"""Djinni (djinni.co) — IT job board of the Ukrainian / Russian-speaking market, many remote jobs.

Feed: https://djinni.co/jobs/rss/?primary_keyword=<Category> (100 latest jobs; the category is
one of Djinni's "primary keywords" like Python, Golang, DevOps, case-insensitive; an unknown
keyword returns the general feed). robots.txt allows /jobs/; the feed is fetched at most once
an hour per keyword (shared cache) with >= 1 s between requests and filtered locally by query
words. Link back to the job URL. A job page carries schema.org JSON-LD (``fetch``).

Options:
  keywords: mapping "query or word" -> Djinni primary keyword, overrides the built-in guess,
            e.g. {"бэкенд": "Python", "frontend": "React.js"}
  match_all_words: if true, every query word must appear (default: any word)
"""

from __future__ import annotations

import re
from typing import Any

from app.core.text import html_to_text
from app.sources.base import JobSource, SearchQuery, VacancyDraft, safe_map
from app.sources.boards import fetch_feed, fetch_page, pick, rss_items
from app.sources.jsonld import draft_from_html

SITE = "https://djinni.co"
FEED = f"{SITE}/jobs/rss/"
_URL_RE = re.compile(r"djinni\.co/jobs/(\d+)")

# Query phrase/word (lower case) -> Djinni primary keyword. Phrases are checked first.
PHRASES = {
    "data engineer": "Data Engineer", "data scien": "Data Science", "data analyst": "Data Analyst",
    "аналитик данных": "Data Analyst", "business analyst": "Business Analyst", "бизнес-аналитик": "Business Analyst",
    "product manager": "Product Manager", "project manager": "Project Manager", "qa automation": "QA Automation",
    "machine learning": "ML AI", "react native": "React Native",
}
WORDS = {
    "python": "Python", "django": "Python", "fastapi": "Python", "java": "Java", "spring": "Java",
    "javascript": "JavaScript", "js": "JavaScript", "typescript": "JavaScript", "golang": "Golang", "go": "Golang",
    "php": "PHP", "laravel": "Laravel", "symfony": "Symfony", "ruby": "Ruby", "rails": "Ruby", "rust": "Rust",
    "scala": "Scala", "kotlin": "Kotlin", "ios": "iOS", "swift": "iOS", "android": "Android", "flutter": "Flutter",
    "devops": "DevOps", "девопс": "DevOps", "sre": "DevOps", "sysadmin": "Sysadmin", "qa": "QA",
    "тестировщик": "QA", "node": "Node.js", "node.js": "Node.js", "nodejs": "Node.js", "react": "React.js",
    "react.js": "React.js", "vue": "Vue.js", "vue.js": "Vue.js", "angular": "Angular", ".net": ".NET",
    "dotnet": ".NET", "c#": ".NET", "c++": "C++", "cpp": "C++", "sql": "SQL", "fullstack": "Fullstack",
    "full-stack": "Fullstack", "elixir": "Elixir", "unity": "Unity", "salesforce": "Salesforce", "sap": "SAP",
    "embedded": "Embedded", "ml": "ML AI", "ai": "ML AI", "llm": "ML AI", "security": "Security",
    "ui/ux": "UI UX", "ux": "UI UX",
}


def primary_keyword(text: str, overrides: dict[str, Any] | None = None) -> str | None:
    """Best Djinni category for the query, or None (then the general feed is used)."""
    low = " ".join(text.lower().split())
    words = low.split()
    custom = {str(k).lower().strip(): str(v) for k, v in (overrides or {}).items() if v}
    for key in [low, *words]:
        if key in custom:
            return custom[key]
    for phrase, kw in PHRASES.items():
        if phrase in low:
            return kw
    return next((WORDS[w] for w in words if w in WORDS), None)


def item_to_draft(item: dict[str, Any]) -> VacancyDraft | None:
    link = item.get("link") or item.get("guid") or ""
    m = _URL_RE.search(link)
    title = html_to_text(item.get("title") or "")
    if not m or not title:
        return None
    return VacancyDraft(
        source="djinni",
        external_id=m.group(1),
        title=title[:300],
        url=link,
        skills=[c for c in item.get("categories") or []][:10],
        description=html_to_text(item.get("description") or ""),
        published_at=item.get("published"),
    )


class DjinniSource(JobSource):
    name = "djinni"
    title = "Djinni"
    hint = "IT-вакансии, в т.ч. удалёнка"
    checked_by_default = False

    async def search(self, query: SearchQuery, limit: int) -> list[VacancyDraft]:
        overrides = self.cfg.options.get("keywords")
        kw = primary_keyword(query.text, overrides if isinstance(overrides, dict) else None)
        async def load() -> list[VacancyDraft]:
            text = await fetch_feed(FEED, params={"primary_keyword": kw} if kw else None,
                                    use_proxy=self.cfg.use_proxy, source=self.title)
            return safe_map(item_to_draft, rss_items(text, source=self.title), source=self.title)
        # Queries mapping to the same keyword share one parsed feed per run.
        drafts = await self.memoized(("feed", kw), load)
        return pick(drafts, query, limit, all_words=bool(self.cfg.options.get("match_all_words")))

    async def fetch(self, external_id: str) -> VacancyDraft | None:
        if not external_id.isdigit():
            return None
        # Djinni needs some slug after the id and redirects to the canonical one.
        url = f"{SITE}/jobs/{external_id}-vacancy/"
        html = await fetch_page(url, use_proxy=self.cfg.use_proxy, source=self.title)
        if html is None:
            return None
        return draft_from_html(html, source=self.name, external_id=external_id, url=None)

    def external_id_from_url(self, url: str) -> str | None:
        m = _URL_RE.search(url)
        return m.group(1) if m else None
