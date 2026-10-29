"""Tenant-bound application service. API delivery is acknowledged before logging success.

This is application plumbing, not a claim of live platform or 24/7 verification.
Webhooks are authenticated by estate.server before entering this trusted service.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Callable
from urllib.parse import parse_qs, quote, urlsplit

from .admin import StatsLog
from .agent import respond
from .checker import check
from .connectors import Attachment, IncomingMessage, InstagramConnector, TelegramConnector
from .extract import extract
from .ingest import is_complete, parse_post
from .instances import Instance, InstanceRegistry
from .kb import Listing, is_control_command
from .language import conversation_language, detect
from .matcher import Criteria
from .ai import AIUnavailable
from .replies import (HANDOVER, REALTOR_AI_HINT, REALTOR_CONFIRM, REALTOR_MENU, format_listing, is_no, is_start,
                      is_yes, realtor_confirmation, realtor_listings, say)
from .telemetry import LatencyLog


class ConfigurationError(RuntimeError):
    """A required runtime setting is absent; never contains its secret value."""


@dataclass(frozen=True)
class ProcessResult:
    message_id: str
    status: str
    reply: str = ""
    blocked: tuple[str, ...] = ()
    ai: bool = False  # the model wrote this reply
    note: str = ""    # why a model draft was not used; never holds secrets


_TEXT_REQUIRED = {
    "uz-latn": "Hozir ovozli xabarni o'qiy olmayman. Iltimos, so'rovingizni matn bilan yozing.",
    "uz-cyrl": "Ҳозир овозли хабарни ўқий олмайман. Илтимос, сўровингизни матн билан ёзинг.",
    "ru": "Сейчас голосовое сообщение недоступно. Пожалуйста, напишите запрос текстом.",
    "en": "Voice transcription is unavailable. Please send your request as text.",
}


# Replies to the realtor's /publish command: usage, taken, checker, incomplete, done.
_PUBLISH_REPLIES = {
    "uz-latn": ("Yozing: /publish L-001, keyingi qatorda e'lon matni.",
                "E'lon chiqarilmadi: bu joy band.",
                "E'lon chiqarilmadi: xavfsizlik tekshiruvidan o'tmadi, matnni tekshiring.",
                "E'londa tuman, xonalar soni, maydon (m2) va $ narx bo'lishi kerak.",
                "E'lon kanalga joylandi."),
    "uz-cyrl": ("Ёзинг: /publish L-001, кейинги қаторда эълон матни.",
                "Эълон чиқарилмади: бу жой банд.",
                "Эълон чиқарилмади: хавфсизлик текширувидан ўтмади, матнни текширинг.",
                "Эълонда туман, хоналар сони, майдон (м2) ва $ нарх бўлиши керак.",
                "Эълон каналга жойланди."),
    "ru": ("Напишите: /publish L-001, а на следующей строке текст объявления.",
           "Объявление не опубликовано: этот объект занят.",
           "Объявление не опубликовано: не прошло проверку безопасности, проверьте текст.",
           "В объявлении должны быть район, число комнат, площадь (м2) и цена в $.",
           "Объявление опубликовано в канале."),
}


class EstateService:
    """One process owns its registry; all conversation access uses its tenant scope.

    Conversation boundaries are an explicit handover or 30 minutes of inactivity.
    Deals are marked only through an authenticated admin action, never inferred.
    A failed delivery remains retryable. Transport retries are at-least-once;
    no platform-independent exactly-once delivery guarantee is made.
    """

    def __init__(self, registry: InstanceRegistry, stats: StatsLog, latency: LatencyLog,
                 connectors: dict[tuple[str, str], object] | None = None,
                 transcriber=None, audio_loader: Callable | None = None,
                 media_gateway=None, clock: Callable[[], float] = time.time,
                 responder=None) -> None:
        if registry.store is None:
            raise ConfigurationError("The application requires a tenant store")
        self.registry, self.stats, self.latency = registry, stats, latency
        self._connectors = dict(connectors or {})
        self.transcriber, self.audio_loader, self.clock = transcriber, audio_loader, clock
        self.media_gateway = media_gateway
        self.responder = responder  # optional estate.ai.ClaudeResponder; None means rules only
        # Realtor assistant state, per (instance, realtor): recent chat, and one proposed
        # status change waiting for "Ha" / "Yo'q". In memory: a restart forgets both.
        self._realtor_chats: dict[tuple[str, str], list[dict]] = {}
        self._pending_changes: dict[tuple[str, str], tuple[float, list[tuple[str, str]], str]] = {}

    def connector(self, instance: Instance, channel: str):
        key = (instance.id, channel)
        if key in self._connectors:
            return self._connectors[key]
        if channel == "telegram":
            token = os.environ.get(instance.telegram_token_env, "") if instance.telegram_token_env else ""
            if not token:
                raise ConfigurationError("Telegram credentials are not configured")
            connector = TelegramConnector(token, channel=instance.telegram_channel)
            if instance.telegram_channel.startswith("@"):
                connector.resolve_channel()
        elif channel == "instagram":
            token = os.environ.get(instance.instagram_token_env, "") if instance.instagram_token_env else ""
            if not token:
                raise ConfigurationError("Instagram credentials are not configured")
            connector = InstagramConnector(instance.instagram_account, token)
        else:
            raise ValueError("Unsupported channel")
        self._connectors[key] = connector
        return connector

    def receive(self, instance_id: str, channel: str, payload: dict) -> list[ProcessResult]:
        instance = self.registry.get(instance_id)
        if instance is None:
            raise KeyError("Unknown instance")
        connector = self.connector(instance, channel)
        return [self.process(instance_id, channel, event, connector)
                for event in connector.parse_update(payload)]

    def _received_at(self, event: IncomingMessage) -> float:
        now = self.clock()
        value = event.timestamp
        if isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= now:
            return float(value)
        return now

    @staticmethod
    def _criteria(history: list[dict], message: str) -> Criteria:
        values = {"budget_usd": None, "district": None, "rooms": None}
        for text in [h["text"] for h in history if h["role"] == "user"] + [message]:
            current = extract(text)
            for key in values:
                if getattr(current, key) is not None:
                    values[key] = getattr(current, key)
        return Criteria(**values)

    @staticmethod
    def _session(history: list[dict], event: IncomingMessage, received: float,
                 channel: str) -> tuple[str, float, list[dict]]:
        active: list[dict] = []
        previous_user = None
        for record in history:
            if record["role"] == "assistant" and record["message_id"].startswith("handover:"):
                active, previous_user = [], None
                continue
            if record["role"] == "user":
                if previous_user is not None and record["timestamp"] - previous_user > 1800:
                    active = []
                previous_user = record["timestamp"]
            active.append(record)
        if previous_user is not None and received - previous_user > 1800:
            active = []
        users = [record for record in active if record["role"] == "user"]
        anchor = users[0]["message_id"] if users else event.id
        started = users[0]["timestamp"] if users else received
        session = hashlib.sha256(f"{channel}\0{event.client_id}\0{anchor}".encode()).hexdigest()
        return session, min(started, received), active

    def _voice_text(self, instance: Instance, channel: str, connector, event: IncomingMessage) -> str | None:
        audio = next((a for a in event.attachments if a.kind in {"voice", "audio"}), None)
        if not audio or self.transcriber is None:
            return None
        try:
            if self.audio_loader is not None:
                content = self.audio_loader(connector, audio)
            else:
                from .voice import instagram_audio, telegram_file
                if channel == "telegram":
                    token = os.environ.get(instance.telegram_token_env, "") if instance.telegram_token_env else ""
                    content, _ = telegram_file(token, audio.url)
                else:
                    content = instagram_audio(audio.url)
            return self.transcriber.transcribe(content, language="auto").strip() or None
        except Exception:
            # Neither provider errors nor signed media URLs belong in a client reply/log.
            return None

    @staticmethod
    def _attachments(listing: Listing) -> tuple[Attachment, ...]:
        photos = tuple(Attachment("photo", url) for url in listing.photos)
        videos = tuple(Attachment("video", url) for url in getattr(listing, "videos", []))
        return photos + videos

    def _gateway_attachment(self, instance: Instance, listing: Listing, kind: str,
                            index: int, reference: str) -> tuple[Attachment, str]:
        """Exempt only authenticated, freshly generated capability query metadata.

        The original stored reference and the capability's origin/path still pass
        the normal checker; arbitrary external URLs never enter this exemption.
        """
        if not check(reference, instance.kb, instance.realtor_contact).ok:
            raise ValueError("Original listing media failed the independent checker")
        attachment = self.media_gateway.attachment(instance, listing, kind, index, reference)
        parsed = urlsplit(attachment.url)
        origin = urlsplit(self.media_gateway.base_url)
        path = "/media/" + "/".join(quote(value, safe="") for value in
                                   (instance.id, listing.id, kind, str(index)))
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (attachment.kind != kind or parsed.scheme != "https" or parsed.netloc != origin.netloc
                or parsed.path != path or parsed.fragment or set(query) != {"expires", "signature"}
                or any(len(values) != 1 for values in query.values())
                or not re.fullmatch(r"[0-9]{1,12}", query["expires"][0])
                or not re.fullmatch(r"[0-9a-f]{64}", query["signature"][0])
                or not hmac.compare_digest(query["signature"][0], self.media_gateway._signature(
                    path, int(query["expires"][0])))):
            raise ValueError("Invalid generated media capability")
        return attachment, self.media_gateway.base_url + path

    def _deliver(self, connector, instance: Instance, event: IncomingMessage, reply: str, channel: str,
                 lang: str | None = None) -> None:
        # A listing card is delivered with its own media, not another property's album.
        ids = list(dict.fromkeys(re.findall(r"\[(L-\d+)\]", reply, re.I)))
        first_card = re.search(r"\[(L-\d+)\]", reply, re.I)
        # Text before the first card (a model-written message) goes out on its own.
        intro = reply[:first_card.start()].strip() if first_card else reply
        if intro:
            if not check(intro, instance.kb, instance.realtor_contact).ok:
                raise ValueError("Final reply failed the independent checker")
            connector.send(event.client_id, intro)
        if not ids:
            return
        for listing_id in ids:
            listing = instance.kb.get(listing_id.upper())
            if listing is None or not listing.available:
                raise ValueError("Listing availability changed before delivery")
            from .media import public_https_url
            if listing.post_url and not public_https_url(listing.post_url):
                raise ValueError("Original post link is not a public HTTPS URL")
            # Cards use the reply's language, which may come from earlier messages.
            text = format_listing(listing, lang or detect(event.text))
            if not check(text, instance.kb, instance.realtor_contact).ok:
                raise ValueError("Listing card failed the independent checker")
            attachments = self._attachments(listing)
            capability_texts = {}
            if channel == "instagram":
                resolved = []
                for kind, references in (("photo", listing.photos), ("video", listing.videos)):
                    for index, reference in enumerate(references):
                        if reference.startswith("https://"):
                            if not public_https_url(reference):
                                raise ValueError("Listing media must not expose API credentials")
                            resolved.append(Attachment(kind, reference))
                        elif self.media_gateway is not None:
                            attachment, check_text = self._gateway_attachment(instance, listing, kind, index, reference)
                            resolved.append(attachment)
                            capability_texts[attachment.url] = check_text
                        else:
                            raise ConfigurationError("Configure the public media gateway for cross-platform files")
                attachments = tuple(resolved)
            for attachment in attachments:
                if attachment.url.startswith("https://") and not public_https_url(attachment.url):
                    raise ValueError("Listing media must not expose API credentials")
                check_text = capability_texts.get(attachment.url, attachment.url)
                if not check(check_text, instance.kb, instance.realtor_contact).ok:
                    raise ValueError("Listing media failed the independent checker")
            connector.send(event.client_id, text, attachments)

    _CONFIRM_SECONDS = 600

    def _stats_summary(self, instance_id: str) -> dict:
        """Counts only: no client IDs or chat text go to the realtor assistant."""
        totals = self.stats.stats(instance_id)
        since = self.clock() - 86400
        recent = [s for s in self.stats.sessions(instance_id) if s["started_at"] >= since]
        return {"clients_total": totals.clients_served,
                "viewing_or_realtor_requests_total": totals.handovers,
                "closed_deals_total": totals.closed_deals,
                "average_conversation_minutes": round(totals.avg_conversation_seconds / 60, 1),
                "last_24_hours": {"conversations": len(recent),
                                  "clients": len({(s["channel"], s["client_id"]) for s in recent}),
                                  "viewing_or_realtor_requests": sum(s["handed_over"] for s in recent)}}

    def _realtor_assistant(self, instance: Instance, key: tuple[str, str], text: str, lang: str) -> tuple[str, bool]:
        """Model answer for a plain-language realtor message; changes wait for confirmation."""
        chat = self._realtor_chats.setdefault(key, [])
        try:
            draft = self.responder.realtor_draft(instance.kb.all(), self._stats_summary(instance.id),
                                                 chat, text, lang)
        except AIUnavailable:
            return say(REALTOR_MENU, lang), False
        listings = {listing.id: listing for listing in instance.kb.all()}
        changes = [(listing_id, status) for listing_id, status in dict.fromkeys(draft.changes)
                   if listing_id in listings and listings[listing_id].available != (status == "available")]
        reply = draft.reply
        if changes:
            self._pending_changes[key] = (self.clock(), changes, lang)
            reply = (reply + "\n\n" if reply else "") + realtor_confirmation(listings, changes, lang)
        reply = reply or say(REALTOR_MENU, lang)
        chat.extend([{"role": "user", "text": text}, {"role": "assistant", "text": reply}])
        del chat[:-20]
        return reply, True

    def _apply_changes(self, instance: Instance, changes: list[tuple[str, str]], lang: str) -> str:
        # Reuse the command path (and its persistence) with words in the realtor's language.
        _, taken, free, _, _ = REALTOR_CONFIRM["uz-latn" if lang == "both" else lang]
        return "\n".join(instance.kb.apply_control_message(f"{listing_id} {taken if status == 'taken' else free}")
                         for listing_id, status in changes)

    def _control(self, instance: Instance, event: IncomingMessage, connector) -> tuple[str, bool]:
        """Realtor messages: exact commands first, then confirmations, then the assistant."""
        text = event.text.strip()
        key = (instance.id, event.client_id)
        lang = conversation_language(text, self._realtor_chats.get(key, []))
        pending = self._pending_changes.pop(key, None)
        if pending and self.clock() - pending[0] <= self._CONFIRM_SECONDS:
            if is_yes(text):
                return self._apply_changes(instance, pending[1], pending[2]), False
            if is_no(text):
                return say({l: v[4] for l, v in REALTOR_CONFIRM.items()}, pending[2]), False
        if text.split(" ", 1)[0].lower() == "/list":
            return realtor_listings(instance.kb.all(), lang), False
        if not text.startswith("/publish "):
            if is_control_command(text):
                return instance.kb.apply_control_message(text), False
            if self.responder is None or is_start(text):
                menu = say(REALTOR_MENU, lang)
                return (menu + "\n\n" + say(REALTOR_AI_HINT, lang) if self.responder else menu), False
            return self._realtor_assistant(instance, key, text, lang)
        return self._publish(instance, event, connector, text), False

    def _publish(self, instance: Instance, event: IncomingMessage, connector, text: str) -> str:
        first, separator, details = text.partition("\n")
        usage, taken, unsafe, incomplete, published = _PUBLISH_REPLIES[detect(details or text)]
        listing_id = first[len("/publish "):].strip().upper()
        if not separator or not re.fullmatch(r"L-[0-9]+", listing_id):
            return usage
        existing = instance.kb.get(listing_id)
        if existing is not None and not existing.available:
            return taken
        # Check against the unchanged live privacy facts BEFORE inserting a
        # candidate with this ID, so duplicate IDs cannot erase owner metadata.
        if not check(details, instance.kb, instance.realtor_contact).ok:
            return unsafe
        listing = parse_post(listing_id, details,
                             photos=[a.url for a in event.attachments if a.kind == "photo"])
        if existing is not None:
            listing = replace(listing, owner_name=existing.owner_name,
                              owner_phone=existing.owner_phone, status=existing.status)
        listing = replace(listing, videos=[a.url for a in event.attachments if a.kind == "video"])
        if not is_complete(listing):
            return incomplete
        card = format_listing(listing, detect(details))
        # Check against a temporary candidate without changing the tenant's live KB.
        from .kb import KnowledgeBase
        candidate_kb = KnowledgeBase(instance.id, instance.kb.all() + [listing])
        if not check(card, candidate_kb, instance.realtor_contact).ok:
            return unsafe
        connector.publish(card, self._attachments(listing))
        return published

    def process(self, instance_id: str, channel: str, event: IncomingMessage,
                connector=None) -> ProcessResult:
        instance = self.registry.get(instance_id)
        if instance is None:
            raise KeyError("Unknown instance")
        if channel not in {"telegram", "instagram"} or not event.id or not event.client_id:
            raise ValueError("Invalid message envelope")
        scope = self.registry.store.scope(instance_id)
        receipt_id = f"{event.client_id}:{event.id}"
        if scope.event_processed(channel, receipt_id):
            return ProcessResult(event.id, "duplicate")
        connector = connector or self.connector(instance, channel)
        if event.is_listing:
            if channel != "telegram":
                raise ValueError("Unsupported listing event")
            # parse_update binds channel posts to the instance's configured channel.
            # A post that names its own ID (e.g. "L-007") keeps it, so the realtor
            # can later send "L-007 band"; otherwise derive a stable one.
            named = re.search(r"(?<![\w-])L-(\d{1,6})(?!\w)", event.text, re.I)
            digest = int(hashlib.sha256(f"{channel}:{event.client_id}:{event.id}".encode()).hexdigest()[:15], 16)
            listing = parse_post(f"L-{named.group(1)}" if named else f"L-{digest}", event.text, event.post_url,
                                 photos=[a.url for a in event.attachments if a.kind == "photo"])
            listing = replace(listing, videos=[a.url for a in event.attachments if a.kind == "video"])
            if is_complete(listing):
                instance.kb.add(listing)
                status = "ingested"
            else:
                status = "incomplete_listing"
            scope.mark_event_processed(channel, receipt_id)
            return ProcessResult(event.id, status)
        if channel == "telegram" and event.client_id in instance.telegram_admin_ids:
            answer, ai = self._control(instance, event, connector)
            connector.send(event.client_id, answer)
            scope.mark_event_processed(channel, receipt_id)
            return ProcessResult(event.id, "control", answer, ai=ai)
        received = self._received_at(event)
        history = scope.history(channel, event.client_id)
        prior = next((item for item in history if item["role"] == "user" and item["message_id"] == event.id), None)
        if prior is not None:
            received = min(received, prior["timestamp"])
            history = [item for item in history if item is not prior]
        session, started, active_history = self._session(history, event, received, channel)
        text = event.text
        if not text and any(a.kind in {"voice", "audio"} for a in event.attachments):
            text = self._voice_text(instance, channel, connector, event) or ""
        scope.record_message(channel, event.client_id, "user", text,
                             message_id=event.id, timestamp=received)
        self.stats.begin(instance_id, channel, event.client_id, session, at=started)
        ai, note, lang = False, "", None
        if text:
            turn = respond(instance.kb, text, self._criteria(active_history, text),
                           instance.realtor_contact, responder=self.responder, history=active_history)
            reply, blocked, handed_over, ai, note = turn.reply, turn.blocked, turn.handover, turn.ai, turn.note
            lang = turn.lang
        else:
            reply, blocked = _TEXT_REQUIRED[detect(event.text)], []
            handed_over = False
        effective_event = replace(event, text=text)
        if not reply:
            self.latency.record(instance_id, channel, receipt_id, received,
                                max(received, self.clock()), success=False)
            scope.mark_event_processed(channel, receipt_id)
            return ProcessResult(event.id, "blocked", blocked=tuple(blocked), note=note)
        try:
            self._deliver(connector, instance, effective_event, reply, channel, lang)
        except Exception:
            self.latency.record(instance_id, channel, receipt_id, received,
                                max(received, self.clock()), success=False)
            raise
        sent = max(received, self.clock())
        self.latency.record(instance_id, channel, receipt_id, received, sent, success=True)
        self.stats.finish(instance_id, session, at=sent, handed_over=handed_over)
        scope.record_message(channel, event.client_id, "assistant", reply,
                             message_id=("handover:" if handed_over else "reply:") + event.id,
                             timestamp=sent)
        scope.mark_event_processed(channel, receipt_id)
        return ProcessResult(event.id, "sent", reply, tuple(blocked), ai, note)
