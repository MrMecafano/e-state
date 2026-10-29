"""One turn: compose and independently check every candidate, including fallbacks."""
from __future__ import annotations

from .checker import check
from .kb import KnowledgeBase
from .language import detect
from .matcher import Criteria
from .replies import HANDOVER, NO_CONTACT, compose


def handle(kb: KnowledgeBase, message: str, criteria: Criteria, realtor_contact: str) -> tuple[str, list[str]]:
    lang = detect(message)
    reply = compose(kb, message, criteria, lang, realtor_contact)
    verdict = check(reply, kb, realtor_contact)
    if verdict.ok:
        return reply, []
    reasons = list(verdict.reasons)
    # Configuration can itself contain private contacts or payment instructions.
    # A fallback is not privileged: validate it against exactly the same policy.
    for fallback in (HANDOVER[lang].format(contact=realtor_contact), NO_CONTACT[lang]):
        verdict = check(fallback, kb, realtor_contact)
        if verdict.ok:
            return fallback, sorted(set(reasons))
        reasons.extend(verdict.reasons)
    # Even a fixed template can collide with a private owner name. Fail closed;
    # callers must not send an empty string, and retain the reasons for review.
    return "", sorted(set(reasons))
