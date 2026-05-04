import asyncio
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes,
)

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
bot_enabled = True

# ConversationHandler states for /lead
LEAD_NAME, LEAD_PHONE, LEAD_QUESTION = range(3)


# ---- Commands ----

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username or "", user.first_name or "", user.last_name or "", TENANT_ID)
    await update.message.reply_text(
        settings.WELCOME_MESSAGE.format(COMPANY_NAME=settings.COMPANY_NAME)
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"Я ассистент компании {settings.COMPANY_NAME}.\n\n"
        "Команды:\n"
        "/start — начать диалог\n"
        "/lead — оставить заявку\n"
        "/help — помощь\n\n"
        "Задайте вопрос по услугам, условиям или документам компании."
    )
    await update.message.reply_text(text)


# ---- Interactive lead flow ----

async def lead_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Как вас зовут?")
    return LEAD_NAME


async def lead_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["lead_name"] = update.message.text.strip()
    await update.message.reply_text("Укажите ваш телефон:")
    return LEAD_PHONE


async def lead_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["lead_phone"] = update.message.text.strip()
    await update.message.reply_text("Опишите вопрос или комментарий:")
    return LEAD_QUESTION


async def lead_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = context.user_data.get("lead_name", user.first_name or "")
    phone = context.user_data.get("lead_phone", "")
    question = update.message.text.strip()

    db.create_lead(
        TENANT_ID, user.id, user.username or "", name, phone, question,
    )
    if settings.OWNER_TELEGRAM_ID:
        try:
            await context.bot.send_message(
                settings.OWNER_TELEGRAM_ID,
                f"Новый лид:\n"
                f"Имя: {name}\n"
                f"Телефон: {phone}\n"
                f"@{user.username or 'нет'}\n"
                f"Запрос: {question}",
            )
        except Exception:
            pass
    await update.message.reply_text("Заявка принята. Менеджер свяжется с вами.")
    context.user_data.clear()
    return ConversationHandler.END


async def lead_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Заявка отменена. Можете задать вопрос.")
    return ConversationHandler.END


# ---- Stats (owner only) ----

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != settings.OWNER_TELEGRAM_ID:
        return
    today = db.get_today_stats(TENANT_ID)
    month = db.get_month_stats(TENANT_ID)
    dashboard = db.get_dashboard_stats(TENANT_ID)
    active = db.get_active_users(TENANT_ID, days=7)
    conversion = db.get_lead_conversion_rate(TENANT_ID)
    text = (
        f"Статистика {settings.COMPANY_NAME}\n"
        f"Tenant: {TENANT_ID}\n"
        f"Пользователей: {dashboard['users']}\n"
        f"Активных (7д): {active}\n"
        f"Диалогов: {dashboard['messages']}\n"
        f"Лидов: {dashboard['leads']} (сегодня: {dashboard['leads_today']})\n"
        f"Конверсия: {conversion}%\n"
        f"Без ответа: {dashboard['missing_questions']}\n"
        f"Негативный фидбек: {dashboard['negative_feedback']}\n"
        f"Фрагментов в RAG: {vector_store.count(TENANT_ID)}\n"
        f"Сегодня: {today['requests']} запросов, {today['tokens']} токенов\n"
        f"Месяц: {month['requests']} запросов, {month['tokens']} токенов"
    )
    await update.message.reply_text(text)


async def toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_enabled
    if update.effective_user.id != settings.OWNER_TELEGRAM_ID:
        return
    bot_enabled = not bot_enabled
    status = "ВКЛЮЧЕН" if bot_enabled else "ОТКЛЮЧЕН"
    await update.message.reply_text(f"Бот {status}")


# ---- Feedback ----

async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, conv_id_str, rating_str = query.data.split(":")
        conv_id = int(conv_id_str)
        rating = int(rating_str)
        conv = db.get_conversation(conv_id, TENANT_ID)
        q_text = conv["question"] if conv else ""
        a_text = conv["answer"] if conv else ""
        db.add_feedback(TENANT_ID, conv_id, query.from_user.id, rating, q_text, a_text)
        await query.edit_message_reply_markup(reply_markup=None)

        if rating < 0 and q_text:
            db.save_missing_question(TENANT_ID, query.from_user.id, q_text, "negative_feedback")

        label = "Спасибо!" if rating > 0 else "Спасибо за обратную связь. Мы улучшим ответ."
        await query.message.reply_text(label)
    except Exception:
        pass


# ---- Lead button callback ----

async def lead_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["lead_step"] = "name"
    await query.message.reply_text("Как вас зовут?")


# ---- Main message handler ----

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_enabled
    if not bot_enabled or not update.message or not update.message.text:
        return

    user = update.effective_user
    user_id = user.id
    user_name = user.first_name or "клиент"
    text = update.message.text.strip()

    # Handle inline lead flow (from button click)
    lead_step = context.user_data.get("lead_step")
    if lead_step == "name":
        context.user_data["lead_name"] = text
        context.user_data["lead_step"] = "phone"
        await update.message.reply_text("Укажите ваш телефон:")
        return
    elif lead_step == "phone":
        context.user_data["lead_phone"] = text
        context.user_data["lead_step"] = "question"
        await update.message.reply_text("Опишите вопрос или комментарий:")
        return
    elif lead_step == "question":
        name = context.user_data.get("lead_name", user_name)
        phone = context.user_data.get("lead_phone", "")
        db.create_lead(TENANT_ID, user_id, user.username or "", name, phone, text)
        if settings.OWNER_TELEGRAM_ID:
            try:
                await context.bot.send_message(
                    settings.OWNER_TELEGRAM_ID,
                    f"Новый лид:\n"
                    f"Имя: {name}\n"
                    f"Телефон: {phone}\n"
                    f"@{user.username or 'нет'}\n"
                    f"Запрос: {text}",
                )
            except Exception:
                pass
        await update.message.reply_text("Заявка принята. Менеджер свяжется с вами.")
        context.user_data.clear()
        return

    question = text

    ok, limit_msg = check_limits(db)
    if not ok:
        await update.message.reply_text(limit_msg)
        return

    now = datetime.now()
    if user_id in user_last_message and now - user_last_message[user_id] < timedelta(seconds=settings.RATE_LIMIT_SECONDS):
        return
    user_last_message[user_id] = now

    is_new = db.is_new_user(user_id, TENANT_ID)
    db.upsert_user(user_id, user.username or "", user_name, user.last_name or "", TENANT_ID)

    if is_new and settings.OWNER_TELEGRAM_ID and settings.NOTIFY_OWNER:
        try:
            await context.bot.send_message(
                settings.OWNER_TELEGRAM_ID,
                f"Новый пользователь: {user_name} (@{user.username or 'нет'})",
            )
        except Exception:
            pass

    # Typing indicator
    chat_id = update.effective_chat.id
    typing_active = True

    async def keep_typing():
        while typing_active:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())

    try:
        result = await processor.process(
            user_id=user_id,
            user_name=user_name,
            username=user.username or "",
            text=question,
            platform="telegram",
        )

        # Notify owner about auto-detected leads
        if result.lead_detected and settings.OWNER_TELEGRAM_ID:
            try:
                await context.bot.send_message(
                    settings.OWNER_TELEGRAM_ID,
                    f"Лид (авто): {user_name} @{user.username or 'нет'}\n"
                    f"Телефон: {result.lead_phone or 'не указан'}\n"
                    f"Агент: {result.agent_name}\n"
                    f"Запрос: {question}",
                )
            except Exception:
                pass

        # Build response with feedback buttons
        buttons = []
        if result.agent_name not in ("guard", "greeting"):
            buttons.append([
                InlineKeyboardButton("\U0001f44d Помогло", callback_data=f"fb:0:1"),
                InlineKeyboardButton("\U0001f44e Не помогло", callback_data=f"fb:0:-1"),
            ])
            if result.lead_detected or result.agent_name == "sales":
                buttons.append([InlineKeyboardButton("Оставить заявку", callback_data="lead_btn")])
        elif result.confidence == 0 and result.agent_name not in ("guard", "greeting"):
            buttons.append([InlineKeyboardButton("Оставить заявку", callback_data="lead_btn")])

        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        await update.message.reply_text(result.answer, reply_markup=keyboard)
    except Exception as e:
        print(f"Ошибка: {e}")
        await update.message.reply_text(
            "Извините, произошла ошибка. Попробуйте позже или напишите менеджеру."
        )
    finally:
        typing_active = False
        typing_task.cancel()


def _register_handlers(app: Application):
    lead_conv = ConversationHandler(
        entry_points=[CommandHandler("lead", lead_start)],
        states={
            LEAD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, lead_name)],
            LEAD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, lead_phone)],
            LEAD_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, lead_question)],
        },
        fallbacks=[CommandHandler("cancel", lead_cancel)],
    )
    app.add_handler(lead_conv)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("toggle", toggle_command))
    app.add_handler(CallbackQueryHandler(feedback_callback, pattern=r"^fb:"))
    app.add_handler(CallbackQueryHandler(lead_button_callback, pattern=r"^lead_btn"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


def main():
    mode = settings.TELEGRAM_MODE.lower()
    print(f"Бот {settings.COMPANY_NAME} запущен. Tenant={TENANT_ID} Mode={mode}")
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    _register_handlers(app)

    if mode == "webhook" and settings.WEBHOOK_URL:
        print(f"Webhook: {settings.WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=settings.WEBHOOK_PORT,
            url_path=f"/bot/{settings.TELEGRAM_BOT_TOKEN}",
            webhook_url=f"{settings.WEBHOOK_URL}/bot/{settings.TELEGRAM_BOT_TOKEN}",
        )
    else:
        print("Polling mode")
        app.run_polling()


if __name__ == "__main__":
    main()
