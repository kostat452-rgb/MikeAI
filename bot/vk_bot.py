"""VK Bot transport for Mike AI.

Listens for messages in a VK group using Long Poll API
and responds using the same RAG + LLM pipeline as Telegram.
"""
import asyncio
import random
from datetime import datetime, timedelta

import aiohttp

from config.settings import settings
from services.llm_service import LLMService
from rag.vector_store import VectorStore
from bot.database import Database
from bot.shared import check_limits, TENANT_ID
from agents.processor import MessageProcessor

vector_store = VectorStore()
llm = LLMService()
db = Database()
processor = MessageProcessor(db, vector_store, llm)
user_last_message: dict[int, datetime] = {}


class VKBot:
    """VK Community Bot using Long Poll API."""

    API_URL = "https://api.vk.com/method"
    API_VERSION = "5.199"

    def __init__(self):
        self.token = settings.VK_GROUP_TOKEN
        self.group_id = settings.VK_GROUP_ID
        if not self.token or not self.group_id:
            raise RuntimeError(
                "VK_GROUP_TOKEN and VK_GROUP_ID must be set in .env to use VK bot"
            )
        self.session: aiohttp.ClientSession | None = None

    async def _api(self, method: str, **params) -> dict:
        params["access_token"] = self.token
        params["v"] = self.API_VERSION
        if self.session is None:
            self.session = aiohttp.ClientSession()
        async with self.session.get(f"{self.API_URL}/{method}", params=params) as resp:
            data = await resp.json()
            if "error" in data:
                raise Exception(f"VK API error: {data['error']}")
            return data.get("response", {})

    async def send_message(self, peer_id: int, text: str):
        await self._api(
            "messages.send",
            peer_id=peer_id,
            message=text,
            random_id=random.randint(1, 2**31),
        )

    async def set_typing(self, peer_id: int):
        try:
            await self._api("messages.setActivity", peer_id=peer_id, type="typing")
        except Exception:
            pass

    async def _get_user_name(self, user_id: int) -> str:
        try:
            result = await self._api("users.get", user_ids=str(user_id))
            if result and isinstance(result, list):
                return result[0].get("first_name", "клиент")
        except Exception:
            pass
        return "клиент"

    async def handle_message(self, peer_id: int, user_id: int, text: str):
        text = text.strip()
        if not text:
            return

        user_name = await self._get_user_name(user_id)

        # Rate limiting
        now = datetime.now()
        if user_id in user_last_message and now - user_last_message[user_id] < timedelta(seconds=settings.RATE_LIMIT_SECONDS):
            return
        user_last_message[user_id] = now

        # Register user
        db.upsert_user(user_id, f"vk_{user_id}", user_name, "", TENANT_ID)

        # Tariff limits check
        ok, limit_msg = check_limits(db)
        if not ok:
            await self.send_message(peer_id, limit_msg)
            return

        # Send typing indicator
        await self.set_typing(peer_id)

        try:
            result = await processor.process(
                user_id=user_id,
                user_name=user_name,
                username=f"vk_{user_id}",
                text=text,
                platform="vk",
            )
            await self.send_message(peer_id, result.answer)
        except Exception as e:
            print(f"VK Bot error: {e}")
            await self.send_message(peer_id, "Извини, произошла ошибка. Попробуй позже или напиши в поддержку.")

    async def run(self):
        print(f"VK Бот {settings.COMPANY_NAME} запущен. Group={self.group_id}")

        # Get Long Poll server
        lp = await self._api("groups.getLongPollServer", group_id=self.group_id)
        server = lp["server"]
        key = lp["key"]
        ts = lp["ts"]

        while True:
            try:
                if self.session is None:
                    self.session = aiohttp.ClientSession()
                url = f"{server}?act=a_check&key={key}&ts={ts}&wait=25"
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    data = await resp.json()

                if "failed" in data:
                    fail = data["failed"]
                    if fail == 1:
                        ts = data["ts"]
                    elif fail in (2, 3):
                        lp = await self._api("groups.getLongPollServer", group_id=self.group_id)
                        server = lp["server"]
                        key = lp["key"]
                        ts = lp.get("ts", ts)
                    continue

                ts = data.get("ts", ts)
                for update in data.get("updates", []):
                    if update["type"] == "message_new":
                        msg = update["object"]["message"]
                        peer_id = msg["peer_id"]
                        from_id = msg["from_id"]
                        text = msg.get("text", "")
                        if text and from_id > 0:
                            asyncio.create_task(self.handle_message(peer_id, from_id, text))
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"VK Long Poll error: {e}")
                await asyncio.sleep(3)
                try:
                    lp = await self._api("groups.getLongPollServer", group_id=self.group_id)
                    server = lp["server"]
                    key = lp["key"]
                    ts = lp.get("ts", ts)
                except Exception:
                    await asyncio.sleep(5)


def main():
    bot = VKBot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
