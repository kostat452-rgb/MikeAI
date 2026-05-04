"""Central message processor — shared by Telegram, VK, and Widget.

Eliminates code duplication: one pipeline, multiple transports.
"""
from __future__ import annotations

import logging

from agents.base import AgentResult
from agents.router import route, has_contact_info
from agents.prompts import build_agent_prompt
from bot.database import Database
from bot.shared import strip_markdown, build_context, TENANT_ID
from config.settings import settings
from core.guards import detect_intent, validate_query, is_unsafe, normalize_spaces, strip_prompt_injection
from rag.vector_store import VectorStore
from services.llm_service import LLMService

logger = logging.getLogger("agents.processor")

SALES_CTA = "\n\nМогу подсказать по условиям. Напиши 'заявка' — менеджер свяжется."
SELLER_CTA = "\n\nХотите попробовать? Оставьте заявку — менеджер свяжется с вами!"

LOW_CONFIDENCE_MSG = (
    "Я не нашёл точного ответа в базе знаний. "
    "Уточни вопрос или напиши в поддержку."
)

GREETINGS = [
    "Привет! Чем могу помочь?",
    "Привет! Спрашивай, подскажу.",
    "Здравствуй! Какой у тебя вопрос?",
    "Привет! Задавай вопрос, помогу разобраться.",
]


class MessageProcessor:
    """Stateless message processor. Shared by all transports (TG, VK, Widget)."""

    def __init__(self, db: Database, vector_store: VectorStore, llm: LLMService):
        self.db = db
        self.vs = vector_store
        self.llm = llm

    async def process(
        self,
        user_id: int,
        user_name: str,
        username: str,
        text: str,
        platform: str = "telegram",
    ) -> AgentResult:
        """Process a user message and return an AgentResult.

        This is the single entry point for all message handling.
        Transports (TG, VK, Widget) call this and handle the response display.
        """
        question = normalize_spaces(text)

        # --- Guards ---
        if is_unsafe(question):
            return AgentResult(
                answer="Я не могу помочь с этим запросом. Задайте вопрос по услугам компании.",
                agent_name="guard",
            )

        quality = validate_query(question)
        intent = detect_intent(question)

        # LLM fallback for ambiguous intents (skip short messages to save budget)
        if intent == "QUESTION" and len(question) >= 10:
            try:
                llm_intent = await self.llm.classify_intent(question)
                if llm_intent != "QUESTION":
                    intent = llm_intent
            except Exception:
                pass

        if intent == "GREETING":
            import random
            greeting = random.choice(GREETINGS)
            if user_name and user_name != "клиент":
                greeting = greeting.replace("Привет!", f"Привет, {user_name}!").replace(
                    "Здравствуй!", f"Здравствуй, {user_name}!"
                )
            return AgentResult(answer=greeting, agent_name="greeting")

        if intent == "GARBAGE" or not quality.ok:
            self.db.save_missing_question(TENANT_ID, user_id, question, f"bad_query:{quality.reason}")
            return AgentResult(
                answer="Не понял вопрос. Напишите конкретно: что хотите узнать по услугам компании?",
                agent_name="guard",
            )

        if intent == "OFFTOPIC":
            return AgentResult(
                answer=f"Я отвечаю только по базе знаний компании {settings.COMPANY_NAME}. "
                       "Задайте вопрос по услугам или условиям.",
                agent_name="guard",
            )

        # --- Route to agent ---
        agent = route(question, intent)
        logger.info("Routed to %s (intent=%s, user=%s)", agent.name, intent, user_id)

        # --- Auto-detect leads (phone/email in message) ---
        has_contact, phone, email = has_contact_info(question)
        lead_detected = False

        if intent == "LEAD" or has_contact:
            phone_val = phone or ""
            source = "bot_detected" if has_contact else "intent_detected"
            self.db.save_lead(
                TENANT_ID, user_id, username, user_name, question, phone_val,
                source=source,
            )
            lead_detected = True

        # --- Check cache ---
        cached = self.db.get_cached_answer(TENANT_ID, question, ttl_hours=settings.CACHE_TTL_HOURS)
        if cached:
            answer = strip_markdown(cached["answer"])
            return AgentResult(
                answer=answer,
                sources=cached.get("sources", ""),
                agent_name=agent.name,
                lead_detected=lead_detected,
                lead_phone=phone if has_contact else "",
                lead_email=email if has_contact else "",
            )

        # --- RAG search ---
        docs = self.vs.search(question, tenant_id=TENANT_ID, top_k=settings.TOP_K_RESULTS)
        confidence = docs[0]["score"] if docs else 0

        if not docs or confidence < settings.RAG_CONFIDENCE_THRESHOLD:
            self.db.save_missing_question(TENANT_ID, user_id, question, "low_confidence")
            self.db.save_conversation(
                user_id, username, user_name, question, LOW_CONFIDENCE_MSG,
                "", 0, TENANT_ID, intent, confidence,
            )
            return AgentResult(
                answer=LOW_CONFIDENCE_MSG,
                confidence=confidence,
                agent_name=agent.name,
                lead_detected=lead_detected,
                lead_phone=phone if has_contact else "",
                lead_email=email if has_contact else "",
            )

        # --- Build prompt & call LLM ---
        system_prompt = build_agent_prompt(agent.name, user_name)
        context = build_context(docs)
        clean_question = strip_prompt_injection(question)
        full_prompt = f"{context}\n\nВопрос от {user_name}: {clean_question}"

        response, tokens = await self.llm.ask(system_prompt, full_prompt, return_usage=True)
        response = strip_markdown(response)
        sources_list = [doc["source"] for doc in docs]
        sources = ", ".join(sorted(set(sources_list)))

        # --- Sales CTA ---
        add_cta = agent.add_sales_cta or (agent.name == "hybrid" and intent == "BUY")
        if add_cta:
            suffix = SELLER_CTA if settings.BOT_ROLE.lower() == "seller" else SALES_CTA
            response += suffix

        # --- Save conversation ---
        conv_id = self.db.save_conversation(
            user_id, username, user_name, question, response,
            sources, tokens, TENANT_ID, intent, confidence,
        )

        # --- Cache ---
        if intent in ("QUESTION", "BUY") and confidence >= settings.RAG_CONFIDENCE_THRESHOLD:
            self.db.set_cached_answer(TENANT_ID, question, response, sources, ttl_hours=settings.CACHE_TTL_HOURS)

        return AgentResult(
            answer=response,
            sources=sources,
            tokens=tokens,
            confidence=confidence,
            agent_name=agent.name,
            lead_detected=lead_detected,
            lead_phone=phone if has_contact else "",
            lead_email=email if has_contact else "",
        )
