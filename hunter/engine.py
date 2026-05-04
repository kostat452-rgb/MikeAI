"""Lead Hunter Engine — orchestrates scanning, filtering, and storing leads."""
import asyncio
import logging

from bot.database import Database
from config.settings import settings
from hunter.vk_scanner import scan_vk_wall, scan_vk_search
from hunter.tg_scanner import scan_tg_chat
from hunter.ai_filter import classify_lead
from agents.pre_filter import pre_filter_lead

logger = logging.getLogger("hunter.engine")


async def run_hunter_task(db: Database, task: dict):
    """Run a single hunter task: scan → filter → store leads."""
    tid = task["tenant_id"]
    platform = task["platform"]
    target = task["target"]
    keywords = [k.strip() for k in task["keywords"].split(",") if k.strip()]
    task_id = task["id"]
    city = task.get("city", "") or ""

    if not keywords:
        logger.warning("Task %d has no keywords", task_id)
        return 0

    raw_leads = []

    if platform == "vk":
        token = settings.VK_SERVICE_KEY or settings.VK_GROUP_TOKEN
        if not token:
            logger.warning("VK token not configured for task %d", task_id)
            return 0
        if target.startswith("search:"):
            raw_leads = await scan_vk_search(token, keywords, hours_back=24, city=city)
        else:
            raw_leads = await scan_vk_wall(token, target, keywords, hours_back=24)

    elif platform == "telegram":
        api_id = getattr(settings, "TG_API_ID", 0)
        api_hash = getattr(settings, "TG_API_HASH", "")
        session_path = f"./data/tg_session_{tid}"
        if not api_id or not api_hash:
            logger.warning("Telegram API credentials not configured for task %d", task_id)
            return 0
        raw_leads = await scan_tg_chat(api_id, api_hash, session_path, target, keywords)

    else:
        logger.warning("Unknown platform '%s' for task %d", platform, task_id)
        return 0

    saved = 0
    filtered = 0
    for lead in raw_leads:
        if not pre_filter_lead(lead.get("text", ""), keywords):
            filtered += 1
            continue

        label, score = await classify_lead(
            text=lead["text"],
            company=settings.COMPANY_NAME,
            keywords=task["keywords"],
            api_key=settings.OPENROUTER_API_KEY,
        )
        if label == "COLD":
            continue

        was_new = db.save_hunter_lead(
            tenant_id=tid,
            task_id=task_id,
            platform=lead["platform"],
            source_url=lead.get("source_url", ""),
            author_id=lead.get("author_id", ""),
            author_name=lead.get("author_name", ""),
            text=lead.get("text", ""),
            relevance_score=score,
        )
        if was_new:
            saved += 1

    db.update_hunter_task_scan(task_id)
    logger.info(
        "Task %d (%s/%s): found %d raw, pre-filtered %d, saved %d new leads",
        task_id, platform, target, len(raw_leads), filtered, saved,
    )
    return saved


async def run_all_tasks(db: Database, tenant_id: str = None):
    """Run all active hunter tasks (optionally filtered by tenant)."""
    tasks = db.list_hunter_tasks(tenant_id) if tenant_id else []
    if not tenant_id:
        # Get all tenants' tasks
        tenants = db.list_tenants()
        for t in tenants:
            tasks.extend(db.list_hunter_tasks(t["id"]))

    active_tasks = [t for t in tasks if t.get("is_active")]
    total_saved = 0
    for task in active_tasks:
        try:
            saved = await run_hunter_task(db, task)
            total_saved += saved
        except Exception as e:
            logger.error("Hunter task %d failed: %s", task["id"], e)
        await asyncio.sleep(1)  # Be polite with APIs

    return total_saved


async def hunter_loop(db: Database, interval_minutes: int = 30):
    """Background loop that runs hunter tasks periodically."""
    logger.info("Hunter loop started (interval=%d min)", interval_minutes)
    while True:
        try:
            saved = await run_all_tasks(db)
            if saved > 0:
                logger.info("Hunter cycle complete: %d new leads", saved)
        except Exception as e:
            logger.error("Hunter loop error: %s", e)
        await asyncio.sleep(interval_minutes * 60)
