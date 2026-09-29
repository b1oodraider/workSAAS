"""Import every module that registers job handlers, so web, worker and tests see the same set."""

from __future__ import annotations


def load_all() -> None:
    import app.bot.jobs  # noqa: F401  - bot_* job handlers (send results to Telegram)
    import app.services  # noqa: F401  - analysis, search_run, vacancy_import
