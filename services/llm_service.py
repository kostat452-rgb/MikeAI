import os
from pathlib import Path

import aiohttp

from config.settings import settings

VALID_INTENTS = {"QUESTION", "GREETING", "GARBAGE", "OFFTOPIC", "BUY", "LEAD"}

INTENT_SYSTEM_PROMPT = (
    "Ты классификатор интентов пользователя для бизнес-бота.\n"
    "Определи тип запроса. Ответь ОДНИМ словом из списка:\n"
    "QUESTION — вопрос по товарам/услугам компании\n"
    "GREETING — приветствие\n"
    "GARBAGE — мусор, бессмысленный текст\n"
    "OFFTOPIC — не по теме компании\n"
    "BUY — хочет купить/узнать цену/заказать\n"
    "LEAD — хочет оставить заявку/контакт\n"
    "Ответь ТОЛЬКО одним словом."
)


class LLMService:
    def __init__(self):
        key_file = Path(__file__).parent.parent / ".api_key"
        self.api_key = settings.OPENROUTER_API_KEY or os.getenv("OPENROUTER_API_KEY", "")
        if not self.api_key and key_file.exists():
            self.api_key = key_file.read_text(encoding="utf-8").strip()
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY не задан. Добавьте его в .env, не храните ключ в коде.")
        self.model = settings.OPENROUTER_MODEL
        self.api_url = "https://openrouter.ai/api/v1/chat/completions"
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _call(self, messages: list[dict], max_tokens: int = 0,
                    temperature: float = 0.15, return_usage: bool = False):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://mike-ai.local",
            "X-Title": "Mike AI",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or settings.MAX_LLM_TOKENS,
        }
        session = await self._get_session()
        async with session.post(
            self.api_url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=settings.LLM_TIMEOUT_SECONDS),
        ) as resp:
            data_text = await resp.text()
            if resp.status != 200:
                raise Exception(f"API Error {resp.status}: {data_text[:500]}")
            data = await resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            if return_usage:
                usage = data.get("usage", {})
                total_tokens = usage.get("total_tokens", 0)
                if not total_tokens:
                    total_tokens = len(content + str(messages)) // 4
                return content, total_tokens
            return content

    async def ask(self, system_prompt: str, user_message: str, return_usage: bool = False):
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        return await self._call(messages, return_usage=return_usage)

    async def classify_intent(self, user_message: str) -> str:
        """LLM fallback intent classifier. Returns one of VALID_INTENTS."""
        messages = [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        try:
            result = await self._call(messages, max_tokens=10, temperature=0.0)
            intent = result.strip().upper().split()[0] if result.strip() else "QUESTION"
            return intent if intent in VALID_INTENTS else "QUESTION"
        except Exception:
            return "QUESTION"
