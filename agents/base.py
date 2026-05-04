"""Agent base class and data structures."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentResult:
    """Result returned by an agent after processing a message."""
    answer: str
    sources: str = ""
    tokens: int = 0
    confidence: float = 0.0
    lead_detected: bool = False
    lead_phone: str = ""
    lead_email: str = ""
    agent_name: str = ""


@dataclass
class Agent:
    """Lightweight agent definition — just a prompt template + behaviour flags."""
    name: str
    system_prompt: str
    use_rag: bool = True
    collect_lead: bool = False
    add_sales_cta: bool = False
    fallback_message: str = ""
