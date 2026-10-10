"""Salary strings from job boards and Telegram posts -> numbers."""

from __future__ import annotations

import re
from typing import Any

# Re-exported via hh_web; kept for compatibility.
_DIGITS_RE = re.compile(r"\d[\d\s  ]*")
# A number: "150 000", "3,500", "3.5", "200".
_NUMBER_RE = re.compile(r"\d+(?:[\s  ,]\d{3})*(?:[.,]\d+)?")
# Thousands shorthand: "200-300k", "250к", "$3.5k", "200 тыс.", "150 т.р.".
_THOUSANDS_RE = re.compile(r"\d\s*(?:[kк](?![a-zа-яё])|тыс|т\.\s*р\.?)")
# Currency markers -> code (hh.ru codes: RUR for rubles, BYR for Belarusian rubles).
# Word-like markers need word boundaries: "brutto" is not Belarusian rubles.
_CURRENCIES = (
    ("BYR", re.compile(r"\bbyn\b|\bbyr\b|\bbr\b|бел\.?\s*руб")),
    ("USD", re.compile(r"\$|\busd\b|долл")),
    ("EUR", re.compile(r"€|\beur\b|\bевро\b")),  # not "Европа"
    ("GBP", re.compile(r"£|\bgbp\b")),
    ("KZT", re.compile(r"₸|\bkzt\b|тенге")),
    ("UZS", re.compile(r"\buzs\b|\bсум\b")),
    ("RUR", re.compile(r"₽|\brub\b|\brur\b|руб|\bр\.|\bт\.\s*р\b")),
)
# ISO codes the app shows as is; anything else from third-party JSON is treated as unknown.
KNOWN_CURRENCIES = {"RUR", "USD", "EUR", "GBP", "KZT", "UZS", "BYR", "UAH", "PLN", "CHF", "CAD", "AUD",
                    "NZD", "INR", "SGD", "ZAR", "BRL", "MXN", "SEK", "NOK", "DKK", "CZK", "ILS", "AED",
                    "TRY", "GEL", "AMD", "AZN", "KGS", "JPY", "CNY", "HKD"}
_ALIASES = {"RUB": "RUR", "BYN": "BYR", "$": "USD", "€": "EUR", "£": "GBP", "₽": "RUR", "₸": "KZT"}
_FROM_RE = re.compile(r"\b(?:от|from)\b")
_TO_RE = re.compile(r"\b(?:до|up to)\b")


def known_currency(value: Any) -> str | None:
    """'usd' -> 'USD', 'RUB' -> 'RUR'; None for empty or unknown values."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    code = _ALIASES.get(code, code)
    return code if code in KNOWN_CURRENCIES else None


def detect_currency(text: str) -> str | None:
    """The currency mentioned first in ``text``, or None."""
    low = text.lower()
    found = [(m.start(), code) for code, rx in _CURRENCIES if (m := rx.search(low))]
    return min(found)[1] if found else None


def _to_int(token: str, thousands: bool) -> int | None:
    token = re.sub(r"[\s  ]", "", token)
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", token):
        token = token.replace(",", "")  # "3,500" is a thousands separator
    try:
        value = float(token.replace(",", "."))
    except ValueError:
        return None
    # "тыс"/"к" somewhere in the text only scales short numbers: "150 000 ₽ + бонус 50к" stays 150 000.
    # "тыс"/"к" somewhere in the text only scales short numbers: "150 000 ₽ + бонус 50к" stays 150 000.
    return int(value * 1000) if thousands and value < 1000 else int(value)


def parse_salary_text(text: str) -> tuple[int | None, int | None, str | None, bool | None]:
    """'от 150 000 до 250 000 ₽ за месяц, на руки' -> (150000, 250000, 'RUR', False).

    Currency is None when the text names none (the caller must not guess)."""
    if not text:
        return None, None, None, None
    low = text.lower()
    if "на руки" in low or re.search(r"\bnetto\b", low):
        gross = False
    elif "до вычета" in low or re.search(r"\b(?:brutto|gross)\b", low):
        gross = True
    else:
        gross = None
    # "до вычета налогов" means gross, not an upper bound.
    low = re.sub(r"до вычета(\s+налогов)?", " ", low)
    currency = detect_currency(low)
    thousands = bool(_THOUSANDS_RE.search(low))
    nums = [n for n in (_to_int(t, thousands) for t in _NUMBER_RE.findall(low)) if n is not None]
    nums = [n for n in nums if n >= 100]  # drop stray small numbers
    if not nums:
        return None, None, currency, gross
    has_from, has_to = bool(_FROM_RE.search(low)), bool(_TO_RE.search(low))
    if has_to and not has_from and len(nums) == 1:
        return None, nums[0], currency, gross
    if len(nums) >= 2:
        return nums[0], nums[1], currency, gross
    return nums[0], None, currency, gross
