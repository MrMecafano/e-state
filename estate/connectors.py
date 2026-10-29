"""Official Telegram Bot API and Instagram Login Messaging API adapters.

No network work happens at construction or parsing. At an HTTP webhook boundary,
use ``parse_webhook`` (or verify before ``parse_update``); a JSON payload alone
cannot establish authenticity. Control-DM authorization belongs to the caller.
Telegram channel posts are emitted only for the configured channel. Deduplicate
message IDs in the caller (Telegram IDs are scoped to a chat); edits are ignored.

``Attachment.url`` is a public HTTPS URL, or, for Telegram incoming media, a bare
Telegram file_id reusable by the same bot. No media URLs are fetched locally.
Voice metadata is retained for a caller's explicit unsupported-voice fallback;
voice sending, downloading and transcription are deliberately not implemented.

Inject a callable ``transport(request: urllib.request.Request, *, timeout: float)
-> tuple[int, bytes]`` for offline use. The default transport uses only fixed
HTTPS API origins, disables redirects and environment proxies, and bounds reads.
Injected transports are trusted to obey the same no-redirect/timeout contract.
Instagram uses Instagram Login (graph.instagram.com), not Facebook Login tokens.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import math
import re
from collections.abc import Mapping
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


@dataclass(frozen=True)
class Attachment:
    kind: str
    url: str


@dataclass(frozen=True)
class IncomingMessage:
    id: str
    client_id: str
    text: str
    timestamp: float | None = None
    attachments: tuple[Attachment, ...] = ()
    is_control: bool = False
    is_listing: bool = False
    post_url: str = ""


class ConnectorError(RuntimeError):
    """Sanitized transport, API, or webhook error; never contains server bodies."""


_TIMEOUT = 10.0
_MAX_BODY = 2 * 1024 * 1024
_FILE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}\Z")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(request: Request, *, timeout: float) -> tuple[int, bytes]:
    # No bearer header or Telegram token path may follow even a same-origin 30x.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(_MAX_BODY + 1)
    except HTTPError as error:
        status = error.code
        error.close()
        return status, b""


def _post(platform: str, url: str, payload: dict, headers: dict, transport,
          timeout: float = _TIMEOUT) -> dict:
    # Construct/send/decode inside the boundary: urllib exceptions often quote a
    # URL containing Telegram's token, and JSON errors can quote raw body data.
    try:
        request = Request(url, json.dumps(payload).encode("utf-8"),
                          {"Content-Type": "application/json", **headers}, method="POST")
        status, body = transport(request, timeout=timeout)
        if type(status) is not int or not 200 <= status < 300:
            raise ValueError
        if not isinstance(body, bytes) or len(body) > _MAX_BODY:
            raise ValueError
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError
    except Exception:
        raise ConnectorError(f"{platform} request failed") from None
    return result


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _id(value) -> str:
    if type(value) is int:
        return str(value)
    return value if isinstance(value, str) else ""


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _timestamp(value, divisor: int = 1) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value) / divisor
        return number if math.isfinite(number) else None
    except (OverflowError, ValueError):
        return None


def _header(headers: Mapping[str, str], name: str) -> str:
    values = [v for k, v in headers.items() if isinstance(k, str) and k.lower() == name.lower()]
    return values[0] if len(values) == 1 and isinstance(values[0], str) else ""


def _equal(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except UnicodeError:
        return False


def _webhook_json(body: bytes) -> dict:
    try:
        if not isinstance(body, bytes) or len(body) > _MAX_BODY:
            raise ValueError
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ConnectorError("Invalid webhook payload") from None
    return payload


def verify_meta_subscription(query: Mapping[str, str], verify_token: str) -> str | None:
    """Return hub.challenge for an authenticated Meta GET subscription, else None."""
    supplied = query.get("hub.verify_token")
    challenge = query.get("hub.challenge")
    if (verify_token and isinstance(supplied, str) and isinstance(challenge, str)
            and query.get("hub.mode") == "subscribe" and _equal(supplied, verify_token)):
        return challenge
    return None


def _https_media(url: str) -> bool:
    # The platform, not this process, fetches remote media. Reject explicit local
    # destinations and ambiguous URL spellings; never resolve/download user URLs.
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if (parts.scheme != "https" or not host or not host.isascii() or "\\" in url
                or parts.username is not None or parts.password is not None
                or parts.fragment or parts.port not in (None, 443) or "%" in host):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.lower().split(".")
            return (len(labels) >= 2 and len(host) <= 253
                    and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in labels)
                    and bool(re.search(r"[a-z]", labels[-1]))
                    and labels[-1] not in {"localhost", "local", "internal", "lan", "home", "test", "invalid"}
                    and not host.lower().endswith(".localhost"))
        return address.is_global and not address.is_multicast
    except (ValueError, UnicodeError):
        return False


def _validate_content(text: str, attachments: tuple[Attachment, ...], *, telegram: bool) -> None:
    if not isinstance(text, str) or not isinstance(attachments, tuple) or not (text or attachments):
        raise ValueError("Text or photo/video attachments required")
    if telegram and (len(text) > 4096 or len(attachments) > 10):
        raise ValueError("Telegram text or album limit exceeded")
    for attachment in attachments:
        if (not isinstance(attachment, Attachment) or not isinstance(attachment.kind, str)
                or attachment.kind not in {"photo", "video"}
                or not isinstance(attachment.url, str) or not attachment.url
                or not (_https_media(attachment.url)
                        or (telegram and _FILE_ID.fullmatch(attachment.url)))):
            raise ValueError("Unsupported or unsafe attachment")


def _utf8_chunks(text: str, limit: int = 1000) -> list[str]:
    """Split without losing whitespace or cutting a Unicode code point."""
    chunks = []
    start = size = 0
    try:
        for index, character in enumerate(text):
            width = len(character.encode("utf-8"))
            if size + width > limit:
                chunks.append(text[start:index])
                start, size = index, 0
            size += width
    except UnicodeError:
        raise ValueError("Instagram text must be valid UTF-8") from None
    if start < len(text):
        chunks.append(text[start:])
    return chunks


class TelegramConnector:
    def __init__(self, token: str, channel: str = "", transport=None):
        self._token = token
        self.channel = channel
        self._transport = transport if transport is not None else _http
        self._resolved_channel: tuple[str, str] | None = None

    def resolve_channel(self) -> str:
        """Explicitly resolve an @alias with getChat and pin its numeric channel ID.

        Call during credential-configured startup, before processing updates.
        Numeric channels need no lookup or credentials. Successful alias lookups
        are cached for this configuration; parsing never performs network work.
        """
        if not isinstance(self.channel, str):
            raise ValueError("Telegram channel required")
        if re.fullmatch(r"-?[0-9]+", self.channel):
            return self.channel
        if not self.channel.startswith("@") or not _USERNAME.fullmatch(self.channel[1:]):
            raise ValueError("Telegram channel required")
        if self._resolved_channel and self._resolved_channel[0] == self.channel.casefold():
            return self._resolved_channel[1]
        if not isinstance(self._token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self._token):
            raise ValueError("Telegram bot credentials required")
        chat = self._call("getChat", {"chat_id": self.channel})
        if chat.get("type") != "channel" or type(chat.get("id")) is not int or chat["id"] >= 0:
            raise ConnectorError("Telegram channel lookup failed")
        channel_id = str(chat["id"])
        self._resolved_channel = (self.channel.casefold(), channel_id)
        return channel_id

    @staticmethod
    def verify_webhook(headers: Mapping[str, str], secret: str) -> bool:
        """Check Telegram's configured secret_token header (not the bot token)."""
        return (isinstance(secret, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret))
                and _equal(_header(headers, "X-Telegram-Bot-Api-Secret-Token"), secret))

    def parse_webhook(self, body: bytes, headers: Mapping[str, str], secret: str) -> list[IncomingMessage]:
        if not self.verify_webhook(headers, secret):
            raise ConnectorError("Telegram webhook authentication failed")
        return self.parse_update(_webhook_json(body))

    def _matches_channel(self, chat: dict) -> bool:
        if not isinstance(self.channel, str) or not self.channel:
            return False
        if re.fullmatch(r"-?[0-9]+", self.channel):
            return self.channel == _id(chat.get("id"))
        if self._resolved_channel and self._resolved_channel[0] == self.channel.casefold():
            return self._resolved_channel[1] == _id(chat.get("id"))
        username = _text(chat.get("username"))
        return (self.channel.startswith("@") and bool(_USERNAME.fullmatch(self.channel[1:]))
                and bool(_USERNAME.fullmatch(username))
                and self.channel[1:].casefold() == username.casefold())

    def parse_update(self, payload: dict) -> list[IncomingMessage]:
        """Pure normalization of an already authenticated update; never trusts control flags."""
        payload = _dict(payload)
        listing = "channel_post" in payload
        message = _dict(payload.get("channel_post" if listing else "message"))
        chat = _dict(message.get("chat"))
        if listing:
            if chat.get("type") != "channel" or not self._matches_channel(chat):
                return []
            client_id = _id(chat.get("id"))
        else:
            sender = _dict(message.get("from"))
            client_id = _id(sender.get("id"))
            if (chat.get("type") != "private" or sender.get("is_bot")
                    or client_id != _id(chat.get("id")) or message.get("sender_chat")
                    or message.get("is_echo") or message.get("outgoing")
                    or message.get("is_outgoing")):
                return []
        message_id = _id(message.get("message_id"))
        if not client_id or not message_id:
            return []
        attachments = []
        photos = [p for p in _list(message.get("photo")) if isinstance(p, dict)
                  and isinstance(p.get("file_id"), str) and _FILE_ID.fullmatch(p["file_id"])]
        if photos:
            def area(photo):
                width, height = photo.get("width"), photo.get("height")
                return width * height if type(width) is int and type(height) is int else 0
            attachments.append(Attachment("photo", max(photos, key=area)["file_id"]))
        for kind in ("video", "voice"):
            file_id = _dict(message.get(kind)).get("file_id")
            if isinstance(file_id, str) and _FILE_ID.fullmatch(file_id):
                attachments.append(Attachment(kind, file_id))
        text = _text(message.get("text")) or _text(message.get("caption"))
        if not text and not attachments:
            return []
        post_url = ""
        if listing:
            username = _text(chat.get("username"))
            if _USERNAME.fullmatch(username):
                post_url = f"https://t.me/{username}/{message_id}" if message_id.isdecimal() else ""
            elif re.fullmatch(r"-100[0-9]+", client_id) and message_id.isdecimal():
                post_url = f"https://t.me/c/{client_id[4:]}/{message_id}"
        return [IncomingMessage(message_id, client_id, text, _timestamp(message.get("date")),
                                tuple(attachments), is_listing=listing, post_url=post_url)]

    def _call(self, method: str, payload: dict, timeout: float = _TIMEOUT) -> dict | list:
        result = _post("Telegram", f"https://api.telegram.org/bot{self._token}/{method}",
                       payload, {}, self._transport, timeout)
        value = result.get("result")
        listed = method in {"sendMediaGroup", "getUpdates"}
        if (result.get("ok") is not True
                or (listed and (not isinstance(value, list) or not all(isinstance(x, dict) for x in value)
                                or (method == "sendMediaGroup" and not value)))
                or (not listed and not isinstance(value, dict))):
            raise ConnectorError("Telegram API request failed")
        return value

    def _require_credentials(self) -> None:
        if not isinstance(self._token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self._token):
            raise ValueError("Telegram bot credentials required")

    def get_me(self) -> dict:
        """Return the bot's own User object; a cheap check that the token works."""
        self._require_credentials()
        return self._call("getMe", {})

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict]:
        """Long-poll for updates when no public webhook exists (local runs only).

        Pass the last handled update_id + 1 as the next offset to confirm a batch.
        Telegram refuses this while a webhook is set or another poller is running.
        """
        self._require_credentials()
        payload = {"timeout": timeout, "allowed_updates": ["message", "channel_post"]}
        if offset is not None:
            payload["offset"] = offset
        # The HTTP read must outlast Telegram's own long-poll wait.
        return self._call("getUpdates", payload, timeout + _TIMEOUT)

    def _deliver(self, client_id: str, text: str, attachments: tuple[Attachment, ...]) -> dict | list:
        if not isinstance(self._token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self._token):
            raise ValueError("Telegram bot credentials required")
        if not isinstance(client_id, str) or not re.fullmatch(r"-?[0-9]+|@[A-Za-z0-9_]{5,32}", client_id):
            raise ValueError("Telegram chat ID required")
        _validate_content(text, attachments, telegram=True)
        results = []
        caption = text if len(text) <= 1024 else ""
        if text and (not attachments or not caption):
            results.append(self._call("sendMessage", {"chat_id": client_id, "text": text}))
        if len(attachments) == 1:
            attachment = attachments[0]
            body = {"chat_id": client_id, attachment.kind: attachment.url}
            if caption:
                body["caption"] = caption
            results.append(self._call("sendPhoto" if attachment.kind == "photo" else "sendVideo", body))
        elif attachments:
            media = [{"type": a.kind, "media": a.url} for a in attachments]
            if caption:
                media[0]["caption"] = caption
            results.extend(self._call("sendMediaGroup", {"chat_id": client_id, "media": media}))
        return results[0] if len(results) == 1 else results

    def send(self, client_id: str, text: str, attachments: tuple[Attachment, ...] = ()) -> None:
        self._deliver(client_id, text, attachments)

    def publish(self, text: str, attachments: tuple[Attachment, ...] = ()) -> dict | list:
        """Publish to the configured channel; return Message or list[Message].

        Up to ten photo/video items form an album. Text over the 1024-character
        caption limit is sent separately before media (4096-character max).
        Multi-call sends are not atomic; errors are not retried automatically.
        """
        if not isinstance(self.channel, str) or not re.fullmatch(r"-?[0-9]+|@[A-Za-z0-9_]{5,32}", self.channel):
            raise ValueError("Telegram channel required")
        channel = self.channel
        if self._resolved_channel and self._resolved_channel[0] == channel.casefold():
            channel = self._resolved_channel[1]
        return self._deliver(channel, text, attachments)


class InstagramConnector:
    def __init__(self, account_id: str, token: str, api_version: str = "v24.0", transport=None):
        self.account_id = account_id
        self._token = token
        self.api_version = api_version
        self._transport = transport if transport is not None else _http

    @staticmethod
    def verify_webhook(body: bytes, headers: Mapping[str, str], app_secret: str) -> bool:
        """Verify X-Hub-Signature-256 over the original bytes, before decoding JSON."""
        signature = _header(headers, "X-Hub-Signature-256")
        if (not isinstance(body, bytes) or not isinstance(app_secret, str) or not app_secret
                or not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", signature)):
            return False
        expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature[7:].lower(), expected)

    def parse_webhook(self, body: bytes, headers: Mapping[str, str], app_secret: str) -> list[IncomingMessage]:
        if not self.verify_webhook(body, headers, app_secret):
            raise ConnectorError("Instagram webhook authentication failed")
        return self.parse_update(_webhook_json(body))

    def parse_update(self, payload: dict) -> list[IncomingMessage]:
        payload = _dict(payload)
        if payload.get("object") != "instagram" or not self.account_id:
            return []
        messages = []
        for entry in _list(payload.get("entry")):
            entry = _dict(entry)
            if _id(entry.get("id")) != self.account_id:
                continue
            for event in _list(entry.get("messaging")):
                event = _dict(event)
                message = _dict(event.get("message"))
                sender = _id(_dict(event.get("sender")).get("id"))
                recipient = _id(_dict(event.get("recipient")).get("id"))
                message_id = _id(message.get("mid"))
                if (not sender or sender == self.account_id or recipient != self.account_id
                        or not message_id or message.get("is_echo") or event.get("is_echo")
                        or message.get("is_deleted") or event.get("is_group") or message.get("is_group")):
                    continue
                attachments = []
                for item in _list(message.get("attachments")):
                    item = _dict(item)
                    kind = {"image": "photo", "video": "video", "audio": "voice"}.get(_text(item.get("type")))
                    url = _dict(item.get("payload")).get("url")
                    if kind and _https_media(url):
                        attachments.append(Attachment(kind, url))
                text = _text(message.get("text"))
                if text or attachments:
                    messages.append(IncomingMessage(message_id, sender, text,
                                                    _timestamp(event.get("timestamp"), 1000), tuple(attachments)))
        return messages

    def send(self, client_id: str, text: str, attachments: tuple[Attachment, ...] = ()) -> None:
        """Send UTF-8 text chunks of at most 1000 bytes, then attachments in order."""
        if (not isinstance(self._token, str) or not re.fullmatch(r"[!-~]+", self._token)
                or not isinstance(self.account_id, str) or not re.fullmatch(r"[0-9]+", self.account_id)):
            raise ValueError("Instagram account ID and credentials required")
        if not isinstance(self.api_version, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+", self.api_version):
            raise ValueError("Instagram API version required")
        if not isinstance(client_id, str) or not re.fullmatch(r"[0-9]+", client_id):
            raise ValueError("Instagram recipient ID required")
        _validate_content(text, attachments, telegram=False)
        parts = [{"text": chunk} for chunk in _utf8_chunks(text)]
        parts.extend({"attachment": {"type": "image" if a.kind == "photo" else "video",
                                      "payload": {"url": a.url}}} for a in attachments)
        for message in parts:
            result = _post("Instagram", f"https://graph.instagram.com/{self.api_version}/{self.account_id}/messages",
                           {"recipient": {"id": client_id}, "message": message},
                           {"Authorization": f"Bearer {self._token}"}, self._transport)
            if "error" in result or not isinstance(result.get("message_id"), str) or not result["message_id"]:
                raise ConnectorError("Instagram API request failed")
