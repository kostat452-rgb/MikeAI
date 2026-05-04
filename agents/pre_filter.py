"""Rules-based pre-filter for hunter leads.

Filters out obvious junk BEFORE calling the LLM classifier.
Saves 70-80% of LLM costs by rejecting garbage early.
"""
from __future__ import annotations

import re

SPAM_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"(?:подпис|лайк|репост).*(?:розыгрыш|конкурс|акци)",
        r"(?:розыгрыш|конкурс|акци).*(?:подпис|лайк|репост)",
        r"реклам[аы]",
        r"https?://t\.me/\S+",  # telegram channel promo
        r"(?:скидк|промо|купон).*(?:\d+%)",
        r"(?:пишите в лс|пишите в личку).*(?:продам|продаю)",
    ]
]

STOP_WORDS = {
    "конкурс", "розыгрыш", "репост", "лайк", "подписка",
    "продам авто", "куплю авто", "сдам квартиру", "сниму квартиру",
    "работа вахтой",
}

MIN_TEXT_LENGTH = 15
MAX_TEXT_LENGTH = 2000


def pre_filter_lead(text: str, keywords: list[str] | None = None) -> bool:
    """Return True if the text should be sent to LLM for classification.

    Return False to reject (skip LLM call, save money).
    """
    if not text or not text.strip():
        return False

    text_clean = text.strip()

    if len(text_clean) < MIN_TEXT_LENGTH:
        return False

    if len(text_clean) > MAX_TEXT_LENGTH:
        text_clean = text_clean[:MAX_TEXT_LENGTH]

    low = text_clean.lower()

    if any(sw in low for sw in STOP_WORDS):
        return False

    if any(p.search(text_clean) for p in SPAM_PATTERNS):
        return False

    # Must contain at least 2 real words
    words = re.findall(r"[а-яА-ЯёЁa-zA-Z]{3,}", text_clean)
    if len(words) < 2:
        return False

    return True
