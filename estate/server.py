"""Authenticated development HTTP adapter for the application service.

Not a production deployment: use a managed HTTPS reverse proxy and operational
monitoring before accepting real webhook traffic. All secrets are environment
references and are intentionally absent from the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .admin import StatsLog
from .instances import InstanceRegistry
from .kb import Listing
from .service import ConfigurationError, EstateService
from .storage import TenantStore
from .telemetry import LatencyLog

MAX_BODY = 1024 * 1024


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"


def _secure_equal(value: str, expected: str) -> bool:
    return (isinstance(value, str) and isinstance(expected, str)
            and hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8")))


def response(status: int, value: dict | list) -> Response:
    return Response(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode())


class Application:
    def __init__(self, service: EstateService, admin_token: str) -> None:
        if not admin_token or len(admin_token) < 24:
            raise ConfigurationError("Set a unique admin token of at least 24 characters")
        self.service, self.admin_token = service, admin_token

    def _authorized(self, headers: dict[str, str]) -> bool:
        return _secure_equal(headers.get("authorization", ""), "Bearer " + self.admin_token)

    @staticmethod
    def _json(body: bytes) -> dict:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def dispatch(self, method: str, target: str, headers: dict[str, str], body: bytes = b"") -> Response:
        headers = {key.lower(): value for key, value in headers.items()}
        if len(body) > MAX_BODY:
            return response(413, {"error": "Request is too large"})
        parsed = urlsplit(target)
        path = parsed.path
        if method == "GET" and path == "/health":
            return response(200, {"status": "ok", "deployment_verified": False})
        if method == "GET" and path in {"/admin", "/admin/"}:
            return Response(200, Path(__file__).with_name("admin_panel.html").read_bytes(),
                            "text/html; charset=utf-8")
        if path.startswith("/admin/") and not self._authorized(headers):
            return response(401, {"error": "Admin authorization required"})
        try:
            if path.startswith("/admin/"):
                return self._admin(method, path, body)
            if path.startswith("/media/") and method == "GET":
                if self.service.media_gateway is None:
                    return response(404, {"error": "Not found"})
                from .media import MediaError
                try:
                    content, mime = self.service.media_gateway.fetch(path, parse_qs(parsed.query, keep_blank_values=True))
                    return Response(200, content, mime)
                except MediaError:
                    return response(404, {"error": "Media is unavailable"})
            parts = path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "webhooks" or parts[2] not in {"telegram", "instagram"}:
                return response(404, {"error": "Not found"})
            _, instance_id, channel = parts
            instance = self.service.registry.get(instance_id)
            if instance is None:
                return response(404, {"error": "Not found"})
            secret = os.environ.get(instance.webhook_secret_env, "") if instance.webhook_secret_env else ""
            if not secret:
                return response(503, {"error": "Webhook verification is not configured"})
            if method == "GET" and channel == "instagram":
                query = parse_qs(parsed.query, keep_blank_values=True)
                token = query.get("hub.verify_token", [""])
                challenge = query.get("hub.challenge", [""])
                if (query.get("hub.mode") != ["subscribe"] or len(token) != 1
                        or len(challenge) != 1 or len(challenge[0]) > 256
                        or not _secure_equal(token[0], secret)):
                    return response(403, {"error": "Verification failed"})
                return Response(200, challenge[0].encode(), "text/plain; charset=utf-8")
            if method != "POST":
                return response(405, {"error": "Method not allowed"})
            if channel == "telegram":
                if not _secure_equal(headers.get("x-telegram-bot-api-secret-token", ""), secret):
                    return response(403, {"error": "Verification failed"})
            else:
                app_secret = os.environ.get(instance.instagram_app_secret_env, "") if instance.instagram_app_secret_env else ""
                if not app_secret:
                    return response(503, {"error": "Meta app verification is not configured"})
                expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
                if not _secure_equal(headers.get("x-hub-signature-256", ""), expected):
                    return response(403, {"error": "Verification failed"})
            results = self.service.receive(instance_id, channel, self._json(body))
            return response(200, {"events": [{"id": item.message_id, "status": item.status} for item in results]})
        except ConfigurationError:
            return response(503, {"error": "Required runtime credentials or settings are missing"})
        except KeyError:
            return response(404, {"error": "Record not found in this instance"})
        except (ValueError, TypeError, json.JSONDecodeError):
            return response(400, {"error": "Invalid request data"})
        except Exception:
            # Provider/SQLite errors may carry tokens, payloads or local paths.
            # Do not return or log them. A 502 keeps failed webhooks retryable.
            return response(502, {"error": "Operation failed; no success was recorded"})

    def _admin(self, method: str, path: str, body: bytes) -> Response:
        registry = self.service.registry
        if path == "/admin/instances":
            if method == "GET":
                return response(200, [{"id": instance.id, "realtor_name": instance.realtor_name,
                                       "telegram_channel": instance.telegram_channel,
                                       "instagram_account": instance.instagram_account}
                                      for instance in registry.all()])
            if method == "POST":
                data = self._json(body)
                allowed = {"instance_id", "realtor_name", "telegram_channel", "instagram_account",
                           "listing_source", "realtor_contact", "telegram_token_env", "instagram_token_env",
                           "webhook_secret_env", "instagram_app_secret_env", "telegram_admin_ids"}
                if not set(data) <= allowed:
                    raise ValueError("Only supported non-secret instance settings are allowed")
                import re
                if not isinstance(data.get("instance_id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", data["instance_id"]):
                    raise ValueError("Instance ID must be a URL-safe slug")
                if "telegram_admin_ids" in data:
                    if not isinstance(data["telegram_admin_ids"], list):
                        raise ValueError("Expected a list of control user IDs")
                    data["telegram_admin_ids"] = tuple(data["telegram_admin_ids"])
                instance = registry.create(**data)
                return response(201, {"id": instance.id, "connection_status": "configured_not_live_verified"})
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["admin", "instances"]:
            return response(404, {"error": "Not found"})
        instance = registry.get(parts[2])
        if instance is None:
            raise KeyError("Unknown instance")
        action = parts[3]
        if method == "GET" and action == "stats":
            return response(200, {"instance_id": instance.id,
                                  "metrics": asdict(self.service.stats.stats(instance.id)),
                                  "latency": self.service.latency.summary(instance.id)})
        if method == "GET" and action == "sessions":
            return response(200, self.service.stats.sessions(instance.id))
        if method == "GET" and action == "history":
            query = self._json(body) if body else {}
            # No cross-tenant parameter exists; use authenticated POST for lookup below.
            return response(405, {"error": "Use POST with channel and client_id"})
        if method == "POST" and action == "history":
            data = self._json(body)
            return response(200, registry.store.scope(instance.id).history(data["channel"], data["client_id"]))
        if method == "POST" and action == "deals":
            self.service.stats.close_deal(instance.id, self._json(body)["session_id"])
            return response(200, {"recorded": True})
        if method == "POST" and action == "control":
            result = instance.kb.apply_control_message(self._json(body)["text"])
            return response(200, {"result": result})
        if method == "POST" and action == "listings":
            data = self._json(body)
            listing = Listing(**data)
            import re
            if not re.fullmatch(r"L-[0-9]+", listing.id) or listing.status not in {"available", "taken"}:
                raise ValueError("Invalid listing ID or status")
            if (not isinstance(listing.rooms, int) or isinstance(listing.rooms, bool)
                    or listing.rooms < 1 or not isinstance(listing.rent_usd, int)
                    or isinstance(listing.rent_usd, bool) or listing.rent_usd <= 0
                    or not isinstance(listing.area_m2, (int, float)) or listing.area_m2 <= 0
                    or not listing.district or not listing.title):
                raise ValueError("Listing needs complete factual fields")
            for values in (listing.photos, listing.videos, listing.amenities):
                if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                    raise ValueError("Listing collections must contain strings")
            instance.kb.add(listing)
            return response(201, {"id": listing.id})
        return response(405, {"error": "Method not allowed"})


def make_handler(app: Application):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # URL queries and request bodies must never become default access logs.

        def _handle(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if self.headers.get("Transfer-Encoding") or length < 0 or length > MAX_BODY:
                self.send_error(413)
                return
            self.connection.settimeout(10)
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    self.send_error(400)
                    return
                result = app.dispatch(self.command, self.path, dict(self.headers), body)
            except (OSError, TimeoutError):
                self.send_error(408)
                return
            self.send_response(result.status)
            self.send_header("Content-Type", result.content_type)
            self.send_header("Content-Length", str(len(result.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(result.body)

        do_GET = do_POST = _handle
    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E-state development webhook/admin server")
    parser.add_argument("--database", default="var/estate.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    admin_token = os.environ.get("ESTATE_ADMIN_TOKEN", "")
    if not admin_token or len(admin_token) < 24:
        parser.error("ESTATE_ADMIN_TOKEN must be set to a unique value of at least 24 characters")
    database = Path(args.database)
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = TenantStore(database)
    stats, latency = StatsLog(database), LatencyLog(database)
    from .voice import WhisperCppTranscriber
    transcriber = WhisperCppTranscriber.from_environment()
    from .media import MediaGateway
    registry = InstanceRegistry(store=store)
    service = EstateService(registry, stats, latency, transcriber=transcriber,
                            media_gateway=MediaGateway.from_environment(registry))
    app = Application(service, admin_token)
    server = HTTPServer((args.host, args.port), make_handler(app))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        latency.close()
        stats.close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
