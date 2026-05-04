"""Cheap local guards for Mike AI.
No paid LLM calls here: fast heuristics before RAG/API.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

CYR_LAT_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ]{2,}")
REPEATED_CHAR_RE = re.compile(r"(.)\1{5,}", re.IGNORECASE)
PHONE_RE = re.compile(
    r"(?:\+7|8)?[\s\-\(]*\d{3}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}"
)

UNSAFE_PATTERNS = [
    r"\b(?:наркотик|закладк|мефедрон|героин|кокаин)\b",
    r"\b(?:экстремизм|терроризм|бомб|взрывчат|оружие)\b",
    r"\b(?:18\+|порно|эротик|интим)\b",
]

INJECTION_PATTERNS = [
    r"ignore (all|previous|system) instructions",
    r"забудь (все|предыдущие) инструкции",
    r"игнорируй (все|предыдущие|системные) инструкции",
    r"ты теперь",
    r"system prompt",
    r"developer message",
    r"act as",
    r"ignore previous instructions",
    r"ignore all instructions",
]

GREETINGS = {
    "привет", "здравствуйте", "добрый день", "добрый вечер",
    "доброе утро", "hello", "hi", "здравствуй", "приветствую",
}

BUY_WORDS = {
    "купить", "заказать", "цена", "стоимость", "оставить заявку",
    "связаться", "менеджер", "консультация", "прайс", "тариф",
}

LEAD_WORDS = BUY_WORDS | {"заявка", "позвонить", "обратный звонок"}

OFFTOPIC_PATTERNS = [
    r"\b(?:погода|анекдот|шутк|расскажи историю|кто ты|кто тебя создал)\b",
    r"\b(?:weather|joke|who are you)\b",
]


@dataclass(frozen=True)
class QualityResult:
    ok: bool
    reason: str = ""


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def words(text: str) -> list[str]:
    return CYR_LAT_WORD_RE.findall((text or "").lower())


def unique_word_ratio(text: str) -> float:
    w = words(text)
    return len(set(w)) / max(len(w), 1)


def has_repeated_garbage(text: str) -> bool:
    return bool(REPEATED_CHAR_RE.search(text or ""))


def is_unsafe(text: str) -> bool:
    low = (text or "").lower()
    return any(re.search(p, low, re.IGNORECASE) for p in UNSAFE_PATTERNS)


def strip_prompt_injection(text: str) -> str:
    clean = text or ""
    for p in INJECTION_PATTERNS:
        clean = re.sub(
            p, "[удалена инструкция из документа]", clean, flags=re.IGNORECASE
        )
    return clean


def validate_query(text: str) -> QualityResult:
    q = normalize_spaces(text)
    low = q.lower()
    if not q:
        return QualityResult(False, "empty")
    if is_unsafe(q):
        return QualityResult(False, "unsafe")
    if len(q) < 3:
        return QualityResult(False, "too_short")
    if has_repeated_garbage(q):
        return QualityResult(False, "repeated_chars")
    w = words(q)
    if len(w) == 0 and len(q) < 12:
        return QualityResult(False, "no_words")
    if len(w) >= 6 and unique_word_ratio(q) < 0.25:
        return QualityResult(False, "low_unique_words")
    return QualityResult(True)


def strip_greeting(text: str) -> str:
    """Remove greeting prefix from text, return the rest."""
    low = text.lower().strip()
    for g in sorted(GREETINGS, key=len, reverse=True):
        if low.startswith(g):
            rest = low[len(g):].lstrip(" ,!.")
            if rest:
                return rest
    return ""


def detect_intent(text: str) -> str:
    q = normalize_spaces(text).lower()
    if not q:
        return "GARBAGE"
    # Pure greeting only if nothing meaningful follows
    rest_after_greeting = strip_greeting(q)
    if (q in GREETINGS or (len(q) <= 20 and any(q.startswith(g) for g in GREETINGS))) and not rest_after_greeting:
        return "GREETING"
    # If greeting + question, analyze the question part
    check_text = rest_after_greeting or q
    if not validate_query(check_text).ok:
        return "GARBAGE"
    if any(re.search(p, check_text, re.IGNORECASE) for p in OFFTOPIC_PATTERNS):
        return "OFFTOPIC"
    if any(word in check_text for word in BUY_WORDS):
        return "BUY"
    if any(word in check_text for word in LEAD_WORDS) or PHONE_RE.search(check_text):
        return "LEAD"
    return "QUESTION"


def validate_document_text(
    text: str, filename: str = "", min_length: int = 200
) -> QualityResult:
    text = normalize_spaces(text)
    if len(text) < max(min_length, 200):
        return QualityResult(
            False,
            f"Документ слишком короткий: минимум {min_length} символов полезного текста",
        )
    if is_unsafe(text):
        return QualityResult(
            False, "Документ содержит запрещённый/рискованный контент"
        )
    if has_repeated_garbage(text[:5000]):
        return QualityResult(
            False, "Документ похож на мусор: много повторяющихся символов"
        )
    w = words(text)
    if len(w) < 40:
        return QualityResult(False, "Слишком мало осмысленных слов")
    if unique_word_ratio(text) < 0.18:
        return QualityResult(
            False, "Документ похож на мусор: низкая уникальность слов"
        )
    for p in INJECTION_PATTERNS:
        if re.search(p, text, re.IGNORECASE):
            return QualityResult(
                False,
                "Документ содержит подозрительные инструкции (prompt injection)",
            )
    name_words = words(filename)
    if filename and len(filename) < 5 and not name_words:
        return QualityResult(False, "Плохое имя файла")
    return QualityResult(True)


def make_cache_key(tenant_id: str, question: str) -> str:
    raw = f"{tenant_id}:{normalize_spaces(question).lower()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
