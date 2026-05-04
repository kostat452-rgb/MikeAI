"""AI-based lead relevance classifier using OpenRouter LLM."""
import logging
import aiohttp

logger = logging.getLogger("hunter.ai_filter")

CLASSIFY_PROMPT = """Ты анализируешь сообщения из соцсетей для поиска потенциальных клиентов.
Компания: {company}
Ключевые слова клиента: {keywords}

Определи, является ли это сообщение потенциальным лидом (человек ищет товар/услугу).

Сообщение: "{text}"

Ответь ОДНИМ словом:
- HOT — человек явно ищет/хочет купить (пример: "подскажите хороший VPN", "кто посоветует мастера")
- WARM — похоже на интерес, но неоднозначно (пример: обсуждение темы, упоминание проблемы)
- COLD — не лид (реклама, спам, просто упоминание слова без интереса)

Ответ:"""


async def classify_lead(text: str, company: str, keywords: str,
                        api_key: str, model: str = "deepseek/deepseek-chat") -> tuple[str, float]:
    """Classify a lead text and return (label, score).

    Returns: ("HOT"|"WARM"|"COLD", 0.0-1.0)
    """
    if not api_key or not text.strip():
        return "WARM", 0.5

    prompt = CLASSIFY_PROMPT.format(company=company, keywords=keywords, text=text[:400])

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 10,
                    "temperature": 0,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                answer = data["choices"][0]["message"]["content"].strip().upper()
    except Exception as e:
        logger.warning("AI classify error: %s", e)
        return "WARM", 0.5

    if "HOT" in answer:
        return "HOT", 0.9
    elif "WARM" in answer:
        return "WARM", 0.6
    else:
        return "COLD", 0.2
