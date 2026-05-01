import os
from pathlib import Path
import aiohttp
from config.settings import settings

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

    async def ask(self, system_prompt: str, user_message: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://mike-ai.local",
            "X-Title": "Mike AI",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.15,
            "max_tokens": settings.MAX_LLM_TOKENS,
        }
        async with aiohttp.ClientSession() as session:
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
                return data["choices"][0]["message"]["content"].strip()
