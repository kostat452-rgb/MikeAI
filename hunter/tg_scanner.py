"""Telegram Lead Hunter — scans public chats/channels for keyword matches.

Requires Telethon and user API credentials (api_id, api_hash).
Falls back to no-op if Telethon is not installed or credentials missing.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("hunter.tg")

try:
    from telethon import TelegramClient
    from telethon.tl.types import PeerChannel, PeerChat
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False


async def scan_tg_chat(api_id: int, api_hash: str, session_path: str,
                       target: str, keywords: list[str],
                       hours_back: int = 24, limit: int = 200) -> list[dict]:
    """Scan a public Telegram chat/channel for messages matching keywords.

    target: username of the chat/channel (e.g. 'auto_msk_chat')
    Returns list of found leads.
    """
    if not TELETHON_AVAILABLE:
        logger.warning("Telethon not installed — TG scanner disabled")
        return []

    if not api_id or not api_hash:
        logger.warning("TG API credentials not configured")
        return []

    results = []
    kw_patterns = [re.compile(re.escape(kw), re.IGNORECASE) for kw in keywords if kw.strip()]
    if not kw_patterns:
        return results

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.start()
        entity = await client.get_entity(target)

        async for msg in client.iter_messages(entity, limit=limit):
            if not msg.text:
                continue
            if msg.date and msg.date < cutoff:
                break
            if any(p.search(msg.text) for p in kw_patterns):
                sender = await msg.get_sender() if msg.sender_id else None
                sender_name = ""
                if sender:
                    first = getattr(sender, "first_name", "") or ""
                    last = getattr(sender, "last_name", "") or ""
                    username = getattr(sender, "username", "") or ""
                    sender_name = f"{first} {last}".strip() or f"@{username}"

                results.append({
                    "author_id": str(msg.sender_id or ""),
                    "author_name": sender_name,
                    "text": msg.text[:500],
                    "source_url": f"https://t.me/{target}/{msg.id}",
                    "platform": "telegram",
                    "type": "message",
                })
    except Exception as e:
        logger.error("TG scan error for %s: %s", target, e)
    finally:
        await client.disconnect()

    return results
