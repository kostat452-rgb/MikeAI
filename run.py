"""
Mike AI — single-process launcher.
Loads embedding model ONCE, runs Telegram bot + Web UI in one process.
Saves ~1.5 GB RAM compared to multi-process approach.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import settings


async def main():
    import uvicorn
    from rag.vector_store import VectorStore
    from bot.database import Database
    from services.llm_service import LLMService

    print("=" * 50)
    print("Mike AI — Запуск (single-process)")
    print("=" * 50)

    # --- Load model ONCE ---
    print("[*] Загрузка модели embeddings...")
    shared_vs = VectorStore()
    shared_db = Database()
    shared_llm = LLMService()
    print("[+] Модель загружена")

    # --- Inject shared instances into web_ui.app ---
    from web_ui import app as web_module
    web_module.vector_store = shared_vs
    web_module.db = shared_db

    # --- Start Telegram bot (non-blocking) ---
    tg_app = None
    if settings.TELEGRAM_BOT_TOKEN:
        from telegram.ext import Application
        import bot.main as bot_mod
        bot_mod.vector_store = shared_vs
        bot_mod.db = shared_db
        bot_mod.llm = shared_llm

        tg_app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
        bot_mod._register_handlers(tg_app)

        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        print(f"[+] Telegram бот запущен (tenant={settings.TENANT_ID})")

    # --- Start VK bot (optional, non-blocking) ---
    vk_task = None
    if settings.VK_ENABLED and settings.VK_GROUP_TOKEN and settings.VK_GROUP_ID:
        try:
            import bot.vk_bot as vk_mod
            vk_mod.vector_store = shared_vs
            vk_mod.db = shared_db
            vk_mod.llm = shared_llm

            from bot.vk_bot import VKBot
            vk_bot = VKBot()
            vk_task = asyncio.create_task(vk_bot.run())
            print(f"[+] VK бот запущен (group={settings.VK_GROUP_ID})")
        except Exception as e:
            print(f"[!] VK бот не запущен: {e}")

    # --- Start Lead Hunter (background, non-blocking) ---
    hunter_task = None
    try:
        from hunter.engine import hunter_loop
        hunter_task = asyncio.create_task(hunter_loop(shared_db, interval_minutes=30))
        print("[+] Лид-хантер запущен (каждые 30 мин)")
    except Exception as e:
        print(f"[!] Лид-хантер не запущен: {e}")

    # --- Start Web UI (uvicorn in-process) ---
    from web_ui.app import app as fastapi_app
    config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
    server = uvicorn.Server(config)

    if settings.WIDGET_ENABLED:
        print("[+] Виджет: http://localhost:8000/widget/code")
    print("[+] Админка: http://localhost:8000")
    print("[+] Суперадминка: http://localhost:8000/owner")
    print("\nCtrl+C для остановки\n")

    try:
        await server.serve()
    finally:
        if tg_app:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        if vk_task:
            vk_task.cancel()
        if hunter_task:
            hunter_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
