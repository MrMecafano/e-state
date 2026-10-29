"""One turn: compose and independently check every candidate, including fallbacks."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .ai import AIUnavailable, Draft
from .checker import check
from .kb import KnowledgeBase
from .language import conversation_language, detect
from .matcher import MAX_RESULTS, Criteria
from .replies import (HANDOVER, NO_CONTACT, compose, format_listing, is_start, mentions_payment, say,
                      wants_handover)

_CARD_MARK = re.compile(r"\[(L-\d+)\]", re.I)


@dataclass(frozen=True)
class Turn:
    reply: str
    blocked: list[str]
    handover: bool
    ai: bool = False
    note: str = ""  # why an AI draft was not used, for the operator log only
    lang: str = "uz-latn"  # reply and card language; "both" until the client shows one


def handle(kb: KnowledgeBase, message: str, criteria: Criteria, realtor_contact: str,
           lang: str | None = None) -> tuple[str, list[str]]:
    lang = lang or detect(message)
    reply = compose(kb, message, criteria, lang, realtor_contact)
    verdict = check(reply, kb, realtor_contact)
    if verdict.ok:
        return reply, []
    reasons = list(verdict.reasons)
    # Configuration can itself contain private contacts or payment instructions.
    # A fallback is not privileged: validate it against exactly the same policy.
    for fallback in (say(HANDOVER, lang, contact=realtor_contact), say(NO_CONTACT, lang)):
        verdict = check(fallback, kb, realtor_contact)
        if verdict.ok:
            return fallback, sorted(set(reasons))
        reasons.extend(verdict.reasons)
    # Even a fixed template can collide with a private owner name. Fail closed;
    # callers must not send an empty string, and retain the reasons for review.
    return "", sorted(set(reasons))


def _from_draft(kb: KnowledgeBase, draft: Draft, lang: str, realtor_contact: str) -> str:
    """Model prose followed by knowledge-base cards; the model never writes a card."""
    # Bracketed IDs mark cards for delivery, so model text may only name them plainly.
    text = _CARD_MARK.sub(r"\1", draft.reply)
    if draft.handover:
        if realtor_contact not in text:
            text = (text + "\n\n" if text else "") + say(HANDOVER, lang, contact=realtor_contact)
        return text
    offered = [listing for listing in (kb.get(i) for i in dict.fromkeys(draft.listing_ids))
               if listing is not None and listing.available and listing.complete][:MAX_RESULTS]
    return "\n\n".join(([text] if text else []) + [format_listing(l, lang) for l in offered])


def _draft_problem(draft: Draft, criteria: Criteria, realtor_contact: str) -> str:
    """Why a model draft breaks the conversation order, or "" if it keeps it.

    Places are listed only once district, rooms and budget are all known (from
    the code's own reading of the chat or the model's), and the realtor's
    contact goes out only in a handover.
    """
    known = Criteria(criteria.budget_usd or draft.budget_usd or None,
                     criteria.district or draft.district or None,
                     criteria.rooms or draft.rooms or None)
    if draft.listing_ids and not draft.handover and known.missing():
        return "AI listed places before knowing district, rooms and budget"
    if not draft.handover and realtor_contact and realtor_contact in draft.reply:
        return "AI gave the realtor contact unasked"
    return ""


def respond(kb: KnowledgeBase, message: str, criteria: Criteria, realtor_contact: str,
            *, responder=None, history: list[dict] | None = None) -> Turn:
    """Let the model draft the reply when one is configured, else use the rules.

    "/start" and payment questions keep fixed replies; the model decides when to hand
    over. A model draft that lists places too early, shares the contact unasked, is
    empty, unavailable or rejected by the checker is replaced by the rule reply.
    """
    note, lang = "", conversation_language(message, history or [])
    if responder is not None and not is_start(message) and not mentions_payment(message):
        try:
            draft = responder.draft(kb.available(), history or [], message, lang)
        except AIUnavailable as error:
            note = f"AI unavailable: {error}"
        else:
            note = _draft_problem(draft, criteria, realtor_contact)
            reply = "" if note else _from_draft(kb, draft, lang, realtor_contact)
            if reply and check(reply, kb, realtor_contact).ok:
                return Turn(reply, [], draft.handover, ai=True, lang=lang)
            note = note or ("AI draft rejected by the checker" if reply else "AI draft was empty")
    reply, blocked = handle(kb, message, criteria, realtor_contact, lang)
    return Turn(reply, blocked, wants_handover(message) or bool(blocked), note=note, lang=lang)
