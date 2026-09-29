"""Salary strings from job boards and Telegram posts -> numbers."""

from __future__ import annotations

import re

_DIGITS_RE = re.compile(r"\d[\d\s  ]*")
_K_SUFFIX_RE = re.compile(r"\d\s*[kк](?![a-zа-яё])")
_CURRENCY = {"₽": "RUR", "руб": "RUR", "$": "USD", "€": "EUR", "₸": "KZT", "сум": "UZS", "br": "BYR"}


def parse_salary_text(text: str) -> tuple[int | None, int | None, str | None, bool | None]:
    """'от 150 000 до 250 000 ₽ за месяц, на руки' -> (150000, 250000, 'RUR', False)."""
    if not text:
        return None, None, None, None
    low = text.lower()
    gross = False if "на руки" in low else (True if "до вычета" in low else None)
    # "до вычета налогов" means gross, not an upper bound.
    low = re.sub(r"до вычета(\s+налогов)?", " ", low)
    if _K_SUFFIX_RE.search(low):
        # Thousands shorthand common in Telegram posts: "200-300k", "250к", "$3.5k".
        nums = [int(float(n.replace(",", ".")) * 1000) for n in re.findall(r"\d+(?:[.,]\d+)?", low)]
    else:
        nums = [int(re.sub(r"\D", "", m)) for m in _DIGITS_RE.findall(low)]
    nums = [n for n in nums if n >= 100]  # drop stray small numbers
    currency = next((code for sym, code in _CURRENCY.items() if sym in low), None)
    if not nums:
        return None, None, currency, gross
    if "от" in low and "до" in low and len(nums) >= 2:
        return nums[0], nums[1], currency, gross
    if low.strip().startswith("до") or (" до " in f" {low}" and "от" not in low):
        return None, nums[0], currency, gross
    if len(nums) >= 2:
        return nums[0], nums[1], currency, gross
    return nums[0], None, currency, gross
