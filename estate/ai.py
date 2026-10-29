"""Optional Claude-written replies (F-02, F-03, F-07, F-08).

There is no retrieval step yet: every free listing of the realtor is sent with
each request. That suits one realtor's few dozen posts; with many more, switch
to RAG (send only the listings that match the request) to keep prompts small.

The model never has the last word. Owner names and phones are never sent, its
draft passes the same independent checker as any reply, listing cards are built
from the knowledge base rather than from model text, and every failure falls
back to the rule-based reply (see agent.respond).

Requires the official SDK (``pip install anthropic``) only when a key is set.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .kb import Listing

DEFAULT_MODEL = "claude-opus-5-5"
MODEL_ENV = "ESTATE_AI_MODEL"
# Models that accept server-side refusal fallbacks in the "default" form.
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

_LANGUAGE_NAMES = {"uz-latn": "Uzbek, Latin script", "uz-cyrl": "Uzbek, Cyrillic script", "ru": "Russian",
                   "both": ("Uzbek (Latin script) and then the same in Russian, because the client has "
                            "not written in either yet")}

_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "listing_ids": {"type": "array", "items": {"type": "string"}},
        "handover": {"type": "boolean"},
        "district": {"type": "string"},
        "rooms": {"type": "integer"},
        "budget_usd": {"type": "integer"},
    },
    "required": ["reply", "listing_ids", "handover", "district", "rooms", "budget_usd"],
    "additionalProperties": False,
}

_INSTRUCTIONS = """You answer rental enquiries in a Tashkent realtor's Telegram and Instagram chats. \
The people writing are looking for a place to rent.

Work in this order:
1. Find out three details: the district, the number of rooms and the monthly budget in USD. Until the \
conversation has given all three, ask only for the missing ones in one short question and leave \
listing_ids empty. Do not suggest any place before that.
2. Once all three are known, recommend at most three places from the list below that fit them. If none \
fit, say so and suggest another district or budget.
3. Only when the client says they want to see a place, or to talk to or contact the realtor, set \
handover to true. Choosing or liking a place is not enough; you may ask whether they would like to \
see it. The realtor's contact is then added to your message automatically, and the client writes to \
the realtor themselves: nothing is passed on, so never say the realtor will contact them or that you \
forwarded the request. Never write contact details yourself.

The free places listed below are the realtor's whole current stock. Places mentioned earlier in the \
chat that are no longer listed have been taken; do not offer them again.

Rules that always hold:
- Mention only places from the list, by id (for example L-005). Never invent places or details.
- Never ask the client for money, a deposit or a prepayment; payment is agreed with the realtor in person.
- You do not know the owners. Never discuss owner names or phone numbers.
- Write only in the language named in the note at the end of the client's latest message, never \
in two languages unless the note asks for both. The listing cards use the same language.

"reply" is your chat message: one to three short sentences, plain text, no markdown. \
"listing_ids" holds the places you recommend in this message. Their full cards (price, district, rooms, \
area, amenities, photos, post link) are sent right after your message, so do not repeat those details, \
prices included. Leave it empty when you are only asking a question. "district", "rooms" and \
"budget_usd" are the details the client has given so far in this conversation: use "", 0 and 0 for any \
not given yet. If the client says a detail does not matter, use "any", 9 or 100000 for it."""


@dataclass(frozen=True)
class Draft:
    reply: str
    listing_ids: tuple[str, ...]
    handover: bool
    # What the model says the client has given so far; empty or 0 when not given.
    district: str = ""
    rooms: int = 0
    budget_usd: int = 0


@dataclass(frozen=True)
class RealtorDraft:
    reply: str
    changes: tuple[tuple[str, str], ...]  # (listing ID, "taken" | "available"), still to be confirmed


_REALTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "changes": {"type": "array", "items": {
            "type": "object",
            "properties": {"listing_id": {"type": "string"},
                           "status": {"type": "string", "enum": ["taken", "available"]}},
            "required": ["listing_id", "status"],
            "additionalProperties": False}},
    },
    "required": ["reply", "changes"],
    "additionalProperties": False,
}

_REALTOR_INSTRUCTIONS = """You are the assistant of a Tashkent realtor, talking with the realtor (the business \
owner) in a private Telegram chat. Clients never see this chat.

You can:
- Answer questions about the realtor's listings and client statistics, using only the data below. If \
something is not in the data (for example who a client was or what they wrote), say you don't have it.
- Propose status changes. When the realtor says a place was rented out or is taken, or is free again, \
put it in "changes" with its id and the new status ("taken" or "available"). Find the place by id or by \
what the realtor says about it (district, title, price, rooms). If more than one place could match, ask \
which one and leave changes empty. Don't propose a change to the status a place already has; say so. \
You never apply changes yourself: the realtor is asked to confirm each one, so don't ask for \
confirmation yourself and never say it is done. "reply" may then be empty or a short remark.
- Draft a listing post (e'lon) when asked: Uzbek (Latin script) first, then Russian, each with a title, \
district, rooms, area in m2, price in $ per month and amenities, then a line "🆔 L-<next unused number>". \
The realtor posts it in the channel; the bot then adds it.
You cannot change prices, delete listings, contact clients or post anything; say so if asked.

Write in the language named in the note at the end of the realtor's latest message (an e'lon draft is \
always Uzbek and Russian). Short plain text, no markdown."""


class AIUnavailable(RuntimeError):
    """No usable draft. The message is a short reason and never holds credentials."""


def listing_facts(listing: Listing) -> dict:
    """What a client may see; the owner's name and phone never leave the process."""
    return {"id": listing.id, "title": listing.title, "district": listing.district,
            "rooms": listing.rooms, "area_m2": listing.area_m2, "rent_usd": listing.rent_usd,
            "amenities": list(listing.amenities), "photos": len(listing.photos),
            "videos": len(listing.videos), "post_url": listing.post_url}


def _messages(history: list[dict], message: str) -> list[dict]:
    """Alternate user/assistant turns that start with the client, ending with ``message``."""
    turns: list[dict] = []
    for role, text in [(h["role"], h["text"]) for h in history] + [("user", message)]:
        if role not in {"user", "assistant"} or not text or (not turns and role != "user"):
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n\n" + text
        else:
            turns.append({"role": role, "content": text})
    return turns


def _reason(error: Exception) -> str:
    """A short, credential-free reason for the operator log."""
    try:
        import anthropic
    except ImportError:
        return type(error).__name__
    if isinstance(error, anthropic.AuthenticationError):
        return "the API key was rejected"
    if isinstance(error, anthropic.RateLimitError):
        return "rate limited"
    if isinstance(error, anthropic.APIStatusError):
        return f"API error {error.status_code}"
    if isinstance(error, anthropic.APIConnectionError):
        return "could not reach the API in time"
    return type(error).__name__


class ClaudeResponder:
    def __init__(self, client, model: str = DEFAULT_MODEL) -> None:
        self.client, self.model = client, model

    @classmethod
    def from_environment(cls) -> "ClaudeResponder | None":
        """None when no key is set. Raises ImportError if a key is set without the SDK."""
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            return None
        import anthropic
        # A client needs a first reply within a minute; the rules answer if the model is slow.
        client = anthropic.Anthropic(timeout=30.0, max_retries=1)
        return cls(client, os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL)

    def _ask(self, system: list[dict], history: list[dict], message: str, lang: str, schema: dict) -> dict:
        """One structured-output request; any failure becomes AIUnavailable."""
        messages = _messages(history, message)
        # The reply language is decided in code so the model's text matches the cards.
        messages[-1]["content"] = [
            {"type": "text", "text": messages[-1]["content"]},
            {"type": "text", "text": f"(Reply language: {_LANGUAGE_NAMES.get(lang, _LANGUAGE_NAMES['uz-latn'])}.)"}]
        request = {
            "model": self.model,
            "max_tokens": 16000,
            "system": system,
            "messages": messages,
            # Chat replies are routine; low effort keeps them quick and cheap.
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        }
        try:
            if self.model in _FALLBACK_MODELS:
                response = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request)
            else:
                response = self.client.messages.create(**request)
        except Exception as error:
            # Every failure has the same outcome (the rule-based reply); only the log differs.
            raise AIUnavailable(_reason(error)) from None
        if response.stop_reason != "end_turn":
            raise AIUnavailable(f"model stopped: {response.stop_reason}")
        try:
            data = json.loads(next(b.text for b in response.content if b.type == "text"))
            if not isinstance(data, dict):
                raise TypeError
        except (StopIteration, ValueError, TypeError):
            raise AIUnavailable("unusable answer") from None
        return data

    def draft(self, listings: list[Listing], history: list[dict], message: str,
              lang: str = "uz-latn") -> Draft:
        facts = [listing_facts(listing) for listing in sorted(listings, key=lambda l: l.id)]
        # Stable text first, then the stock; the cache breakpoint covers both.
        system = [
            # The contact stays out of the prompt; code adds it only to a handover.
            {"type": "text", "text": _INSTRUCTIONS},
            {"type": "text", "text": "Free places (JSON):\n" + json.dumps(facts, ensure_ascii=False, sort_keys=True),
             "cache_control": {"type": "ephemeral"}},
        ]
        data = self._ask(system, history, message, lang, _SCHEMA)
        try:
            reply, ids, handover = data["reply"], data["listing_ids"], data["handover"]
            district, rooms, budget = data["district"], data["rooms"], data["budget_usd"]
            if (not isinstance(reply, str) or not isinstance(handover, bool) or not isinstance(ids, list)
                    or not all(isinstance(x, str) for x in ids) or not isinstance(district, str)
                    or type(rooms) is not int or type(budget) is not int):
                raise TypeError
        except (KeyError, TypeError):
            raise AIUnavailable("unusable answer") from None
        return Draft(reply.strip(), tuple(x.strip().upper() for x in ids), handover,
                     district.strip(), rooms, budget)

    def realtor_draft(self, listings: list[Listing], stats: dict, history: list[dict], message: str,
                      lang: str = "uz-latn") -> RealtorDraft:
        """Answer the realtor; status changes are only proposed, never applied here."""
        facts = [dict(listing_facts(listing), status="available" if listing.available else "taken")
                 for listing in sorted(listings, key=lambda l: l.id)]
        system = [
            {"type": "text", "text": _REALTOR_INSTRUCTIONS},
            {"type": "text", "text": "Listings (JSON):\n" + json.dumps(facts, ensure_ascii=False, sort_keys=True),
             "cache_control": {"type": "ephemeral"}},
            # Statistics change with every client message, so they come after the cached part.
            {"type": "text", "text": "Client statistics (JSON):\n" + json.dumps(stats, sort_keys=True)},
        ]
        data = self._ask(system, history, message, lang, _REALTOR_SCHEMA)
        try:
            reply, changes = data["reply"], data["changes"]
            if not isinstance(reply, str) or not isinstance(changes, list):
                raise TypeError
            proposed = tuple((c["listing_id"].strip().upper(), c["status"]) for c in changes)
            if not all(status in {"taken", "available"} for _, status in proposed):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise AIUnavailable("unusable answer") from None
        return RealtorDraft(reply.strip(), proposed)
