"""One turn of the agent: compose, check, and fall back to handover when blocked (UC-1)."""
from __future__ import annotations

from .checker import check
from .kb import KnowledgeBase
from .language import detect
from .matcher import Criteria
from .replies import HANDOVER, compose


def handle(kb: KnowledgeBase, message: str, criteria: Criteria, realtor_contact: str) -> tuple[str, list[str]]:
    lang = detect(message)
    reply = compose(kb, message, criteria, lang, realtor_contact)
    verdict = check(reply, kb, realtor_contact)
    if verdict.ok:
        return reply, []
    # Checker rejected the reply: hand over to the realtor instead (UC-1 exception course).
    return HANDOVER[lang].format(contact=realtor_contact), verdict.reasons
