"""Short-lived signed media capabilities for Telegram-to-Instagram listing delivery.

Never expose a Telegram /file/bot<TOKEN>/ URL to Meta or clients. The application
resolves a tenant's stored file ID server-side and returns only the media bytes.
"""
from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import re
import time
from urllib.parse import quote, unquote, urlencode, urlsplit

from .connectors import Attachment
from .voice import telegram_file


class MediaError(RuntimeError):
    pass


def public_https_url(reference: str) -> bool:
    """Public references must not contain API credentials or URL userinfo."""
    try:
        decoded = unquote(unquote(reference))
        parsed = urlsplit(decoded)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or host == "api.telegram.org" or parsed.port not in {None, 443}
                or re.search(r"(?:[?&](?:access_token|api_key|bot_token)=|/bot[0-9]+:)", decoded, re.I)):
            return False
        return True
    except (TypeError, ValueError):
        return False


class MediaGateway:
    def __init__(self, registry, base_url: str, signing_key: str, *, clock=time.time,
                 fetcher=telegram_file) -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
                or len(signing_key) < 24):
            raise MediaError("Configure an HTTPS public origin and unique media signing key")
        self.registry, self.base_url = registry, base_url.rstrip("/")
        self._key, self.clock, self.fetcher = signing_key.encode(), clock, fetcher

    @classmethod
    def from_environment(cls, registry):
        origin, key = os.environ.get("ESTATE_PUBLIC_BASE_URL", ""), os.environ.get("ESTATE_MEDIA_SIGNING_KEY", "")
        return cls(registry, origin, key) if origin or key else None

    def _signature(self, path: str, expires: int) -> str:
        return hmac.new(self._key, f"{path}\n{expires}".encode(), hashlib.sha256).hexdigest()

    def attachment(self, instance, listing, kind: str, index: int, reference: str) -> Attachment:
        if reference.startswith("https://"):
            if not public_https_url(reference):
                raise MediaError("A credential-bearing URL is not public listing media")
            return Attachment(kind, reference)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", reference):
            raise MediaError("Unsupported listing media reference")
        expires = int(self.clock()) + 600
        path = "/media/" + "/".join(quote(value, safe="") for value in
                                      (instance.id, listing.id, kind, str(index)))
        query = urlencode({"expires": expires, "signature": self._signature(path, expires)})
        return Attachment(kind, self.base_url + path + "?" + query)

    def fetch(self, path: str, query: dict[str, list[str]]) -> tuple[bytes, str]:
        try:
            if set(query) != {"expires", "signature"} or any(len(value) != 1 for value in query.values()):
                raise ValueError()
            expires = int(query["expires"][0])
            now = self.clock()
            if expires <= now or expires > now + 600:
                raise ValueError()
            if not hmac.compare_digest(query["signature"][0], self._signature(path, expires)):
                raise ValueError()
            parts = path.strip("/").split("/")
            if len(parts) != 5 or parts[0] != "media" or parts[3] not in {"photo", "video"}:
                raise ValueError()
            instance = self.registry.get(parts[1])
            listing = instance.kb.get(parts[2]) if instance else None
            if listing is None or not listing.available or not re.fullmatch(r"[0-9]{1,3}", parts[4]):
                raise ValueError()
            values = listing.photos if parts[3] == "photo" else listing.videos
            reference = values[int(parts[4])]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", reference):
                raise ValueError()
            token = os.environ.get(instance.telegram_token_env, "") if instance.telegram_token_env else ""
            data, source_path = self.fetcher(token, reference, max_bytes=20 * 1024 * 1024)
            mime = mimetypes.guess_type(source_path)[0] or "application/octet-stream"
            expected = "image/" if parts[3] == "photo" else "video/"
            if not mime.startswith(expected):
                raise ValueError()
            return data, mime
        except Exception:
            raise MediaError("Media is unavailable or its capability has expired") from None
