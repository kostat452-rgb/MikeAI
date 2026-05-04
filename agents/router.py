"""Rules-based message router — selects the right agent for each message.

No LLM calls here: fast, free, deterministic.
LLM router can be added later when rules stop covering edge cases.
"""
from __future__ import annotations

import re

from agents.base import Agent
from config.settings import settings

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(
    r"(?:\+7|8)?[\s\-\(]*\d{3}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}"
)

BUY_KEYWORDS = {
    "купить", "заказать", "цена", "стоимость", "сколько стоит",
    "тариф", "прайс", "оплата", "подключить", "попробовать",
    "хочу купить", "как купить", "как оплатить", "скидка",
    "оставить заявку", "связаться", "менеджер", "консультация",
}

LEAD_KEYWORDS = BUY_KEYWORDS | {
    "заявка", "позвонить", "обратный звонок", "мой телефон",
    "перезвоните", "номер телефона",
}

# Agents registry
SUPPORT_AGENT = Agent(
    name="support",
    system_prompt="",  # built dynamically via prompts.py
    use_rag=True,
    collect_lead=False,
    add_sales_cta=False,
)

SALES_AGENT = Agent(
    name="sales",
    system_prompt="",
    use_rag=True,
    collect_lead=True,
    add_sales_cta=True,
)

HYBRID_AGENT = Agent(
    name="hybrid",
    system_prompt="",
    use_rag=True,
    collect_lead=False,
    add_sales_cta=False,  # CTA only when intent=BUY
)


def has_contact_info(text: str) -> tuple[bool, str, str]:
    """Check if message contains phone or email. Returns (found, phone, email)."""
    phone_match = PHONE_RE.search(text)
    email_match = EMAIL_RE.search(text)
    phone = phone_match.group(0) if phone_match else ""
    email = email_match.group(0) if email_match else ""
    return bool(phone or email), phone, email


def route(message: str, intent: str) -> Agent:
    """Select agent based on message content and detected intent.

    Rules priority:
    1. Contact info in message → Sales (auto-collect lead)
    2. BUY/LEAD intent → Sales
    3. BOT_ROLE=seller → Sales for everything
    4. BOT_ROLE=helper → Support for everything
    5. BOT_ROLE=hybrid → Hybrid (default)
    """
    low = message.lower()
    role = settings.BOT_ROLE.lower()

    has_contact, _, _ = has_contact_info(message)
    if has_contact:
        return SALES_AGENT

    if intent in ("BUY", "LEAD"):
        return SALES_AGENT

    if any(kw in low for kw in BUY_KEYWORDS):
        return SALES_AGENT

    if role == "seller":
        return SALES_AGENT
    elif role == "helper":
        return SUPPORT_AGENT
    else:
        return HYBRID_AGENT
