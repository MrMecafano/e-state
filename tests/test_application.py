"""Offline application regressions: real normalization/connectors, synthetic wire ACKs."""
import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from estate.admin import StatsLog
from estate.connectors import ConnectorError, InstagramConnector, TelegramConnector
from estate.instances import InstanceRegistry
from estate.kb import Listing
from estate.server import Application, MAX_BODY
from estate.service import ConfigurationError, EstateService
from estate.storage import TenantStore
from estate.telemetry import LatencyLog

TG_TOKEN = "900001:synthetic_application_test_only"
IG_TOKEN = "synthetic_instagram_application_token_only"
ADMIN_TOKEN = "synthetic_admin_application_token_only_123"
WEBHOOK_SECRET = "synthetic_webhook_secret_only"
META_SECRET = "synthetic_meta_app_secret_only"
FULL_REQUEST = "Yunusobodda 2 xonali kvartira kerak, 600$ gacha"
PHOTO = "https://cdn.example.com/synthetic-flat.jpg"
VIDEO = "https://cdn.example.com/synthetic-flat.mp4"


def telegram(text=FULL_REQUEST, *, client=111, message=42, at=1000, **extra):
    value = {"message_id": message, "date": at, "from": {"id": client, "is_bot": False},
             "chat": {"id": client, "type": "private"}, "text": text}
    value.update(extra)
    return {"update_id": message, "message": value}


def instagram(text=FULL_REQUEST, *, account="100001", client="111", message="42", at=1000,
              attachments=None):
    value = {"mid": message}
    if text is not None:
        value["text"] = text
    if attachments is not None:
        value["attachments"] = attachments
    return {"object": "instagram", "entry": [{"id": account, "messaging": [{
        "sender": {"id": client}, "recipient": {"id": account},
        "timestamp": at * 1000, "message": value}]}]}


class WireTransport:
    def __init__(self):
        self.calls = []
        self.fail_next = False
        self.before_ack = None

    def __call__(self, request, *, timeout):
        body = json.loads(request.data)
        self.calls.append((request.full_url, body, dict(request.header_items()), timeout))
        if self.before_ack:
            self.before_ack()
        if self.fail_next:
            self.fail_next = False
            raise OSError("synthetic_provider_private_body " + TG_TOKEN)
        if request.full_url.startswith("https://api.telegram.org/"):
            result = {"message_id": len(self.calls), "chat": {"id": body["chat_id"]}}
            if request.full_url.endswith("/sendMediaGroup"):
                result = [dict(result, message_id=index + 1) for index in range(len(body["media"]))]
            return 200, json.dumps({"ok": True, "result": result}).encode()
        return 200, json.dumps({"recipient_id": body["recipient"]["id"],
                                "message_id": "synthetic_ack_" + str(len(self.calls))}).encode()


class ApplicationFixture(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket", side_effect=AssertionError("live network forbidden")).start()
        patch.dict(os.environ, {
            "SYNTHETIC_TG_TOKEN": TG_TOKEN, "SYNTHETIC_IG_TOKEN": IG_TOKEN,
            "SYNTHETIC_WEBHOOK_SECRET": WEBHOOK_SECRET, "SYNTHETIC_META_SECRET": META_SECRET,
        }, clear=True).start()
        self.store = TenantStore(":memory:")
        self.stats, self.latency = StatsLog(), LatencyLog()
        for log in (self.store, self.stats, self.latency):
            self.addCleanup(log.close)
        self.registry = InstanceRegistry(store=self.store)
        self.wires, connectors = {}, {}
        for tenant, account, admin in (("a", "100001", "900"), ("b", "100002", "901")):
            instance = self.registry.create(
                tenant, "Synthetic " + tenant, telegram_channel="@synthetic_" + tenant,
                instagram_account=account, realtor_contact="@synthetic_realtor_" + tenant,
                telegram_token_env="SYNTHETIC_TG_TOKEN", instagram_token_env="SYNTHETIC_IG_TOKEN",
                webhook_secret_env="SYNTHETIC_WEBHOOK_SECRET",
                instagram_app_secret_env="SYNTHETIC_META_SECRET", telegram_admin_ids=(admin,))
            instance.kb.add(Listing("L-001", "Tenant " + tenant + " source title", "Yunusobod", 2, 50, 500,
                                    post_url="https://t.me/synthetic_" + tenant + "/1",
                                    owner_name="PrivateOwner" + tenant))
            for channel in ("telegram", "instagram"):
                wire = self.wires[tenant, channel] = WireTransport()
                connectors[tenant, channel] = (TelegramConnector(TG_TOKEN, instance.telegram_channel, wire)
                    if channel == "telegram" else InstagramConnector(account, IG_TOKEN, transport=wire))
        self.now = 1002
        self.service = EstateService(self.registry, self.stats, self.latency, connectors,
                                     clock=lambda: self.now)
        self.app = Application(self.service, ADMIN_TOKEN)

    def request(self, method, path, data=None, *, authorized=True, headers=None, body=None):
        actual_headers = {"Authorization": "Bearer " + ADMIN_TOKEN} if authorized else {}
        actual_headers.update(headers or {})
        if body is None:
            body = b"" if data is None else json.dumps(data).encode()
        return self.app.dispatch(method, path, actual_headers, body)

    def json_request(self, *args, **kwargs):
        reply = self.request(*args, **kwargs)
        return reply.status, json.loads(reply.body)


class EstateServiceIntegrationTest(ApplicationFixture):
    def test_normalized_telegram_text_is_logged_only_after_wire_ack(self):
        wire = self.wires["a", "telegram"]
        wire.before_ack = lambda: self.assertEqual(self.latency.summary("a")["count"], 0)
        result, = self.service.receive("a", "telegram", telegram())
        self.assertEqual(result.status, "sent")
        self.assertEqual(wire.calls[0][0], "https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage")
        self.assertEqual(wire.calls[0][1]["chat_id"], "111")
        self.assertIn("[L-001]", wire.calls[0][1]["text"])
        history = self.store.scope("a").history("telegram", "111")
        self.assertEqual([row["role"] for row in history], ["user", "assistant"])
        self.assertEqual(history[0]["text"], FULL_REQUEST)
        self.assertEqual(self.latency.summary("a"), {
            "count": 1, "failed": 0, "p95_seconds": 2, "under_60_seconds": 1})
        self.assertEqual(self.stats.stats("a").clients_served, 1)

    def test_telegram_photo_uses_real_media_endpoint_and_localized_caption(self):
        instance = self.registry.get("a")
        instance.kb.add(Listing("L-001", "Source title", "Yunusobod", 2, 50, 500, photos=[PHOTO]))
        self.service.receive("a", "telegram", telegram())
        call, = self.wires["a", "telegram"].calls
        self.assertTrue(call[0].endswith("/sendPhoto"))
        self.assertEqual(call[1]["photo"], PHOTO)
        self.assertIn("/oy", call[1]["caption"])
        self.assertEqual(self.latency.summary("a")["count"], 1)

    def test_instagram_text_photo_video_wait_for_all_acks(self):
        self.registry.get("a").kb.add(Listing("L-001", "Source title", "Yunusobod", 2, 50, 500,
                                                   photos=[PHOTO], videos=[VIDEO]))
        wire = self.wires["a", "instagram"]
        wire.before_ack = lambda: self.assertEqual(self.latency.summary("a")["count"], 0)
        self.assertEqual(self.service.receive("a", "instagram", instagram())[0].status, "sent")
        self.assertEqual(len(wire.calls), 3)
        self.assertTrue(all(call[0] == "https://graph.instagram.com/v24.0/100001/messages" for call in wire.calls))
        self.assertIn("text", wire.calls[0][1]["message"])
        self.assertEqual([call[1]["message"]["attachment"]["type"] for call in wire.calls[1:]], ["image", "video"])
        self.assertEqual(self.latency.summary("a")["count"], 1)

    def test_instagram_partial_media_failure_is_not_success_and_remains_retryable(self):
        self.registry.get("a").kb.add(Listing("L-001", "Source title", "Yunusobod", 2, 50, 500,
                                                   photos=[PHOTO], videos=[VIDEO]))
        wire = self.wires["a", "instagram"]
        def fail_second_part():
            self.assertEqual(self.latency.summary("a")["count"], 0)
            if len(wire.calls) == 2:
                wire.fail_next = True
        wire.before_ack = fail_second_part
        with self.assertRaises(ConnectorError):
            self.service.receive("a", "instagram", instagram())
        self.assertEqual(len(wire.calls), 2)
        self.assertEqual(self.latency.summary("a")["failed"], 1)
        self.assertEqual(len(self.store.scope("a").history("instagram", "111")), 1)
        wire.before_ack = None
        self.now = 1070
        self.assertEqual(self.service.receive("a", "instagram", instagram(at=1050))[0].status, "sent")
        self.assertEqual(self.latency.summary("a")["p95_seconds"], 70)
        self.assertEqual(self.latency.summary("a")["failed"], 0)
        self.assertEqual(len(wire.calls), 5)  # retries are explicitly at-least-once

    def test_telegram_token_urls_are_never_forwarded_in_instagram_payloads(self):
        self.registry.get("a").kb.add(Listing("L-001", "Source title", "Yunusobod", 2, 50, 500,
            photos=["https://api.telegram.org/file/bot" + TG_TOKEN + "/photos/synthetic.jpg"]))
        from estate.media import MediaError
        try:
            self.service.receive("a", "instagram", instagram())
        except (MediaError, ConfigurationError, ConnectorError, ValueError):
            pass  # rejecting unsafe source media is an acceptable outcome
        for _url, body, _headers, _timeout in self.wires["a", "instagram"].calls:
            self.assertNotIn(TG_TOKEN, json.dumps(body))
            self.assertNotIn("/file/bot", json.dumps(body))

    def test_equal_message_ids_are_separate_per_client_tenant_channel_and_deduplicate(self):
        for tenant, account in (("a", "100001"), ("b", "100002")):
            for channel in ("telegram", "instagram"):
                for client in (111, 222):
                    payload = (telegram(client=client) if channel == "telegram" else
                               instagram(account=account, client=str(client)))
                    with self.subTest(tenant=tenant, channel=channel, client=client):
                        self.assertEqual(self.service.receive(tenant, channel, payload)[0].status, "sent")
                        self.assertEqual(self.service.receive(tenant, channel, payload)[0].status, "duplicate")
                        history = self.store.scope(tenant).history(channel, str(client))
                        self.assertEqual(len(history), 2)
                        self.assertIn("Tenant " + tenant, history[-1]["text"])
            self.assertEqual(self.latency.summary(tenant)["count"], 4)
            self.assertEqual(self.stats.stats(tenant).clients_served, 4)

    def test_partial_turns_accumulate_criteria_in_one_session(self):
        self.now = 1010
        for message, text in enumerate(("Yunusobodda kvartira kerak", "2 xonali", "600$ gacha"), 1):
            result, = self.service.receive("a", "telegram", telegram(text, message=message, at=1000 + message))
        self.assertIn("[L-001]", result.reply)
        self.assertEqual(len(self.stats.sessions("a")), 1)
        self.assertEqual(len(self.store.scope("a").history("telegram", "111")), 6)

    def test_handover_starts_fresh_criteria_on_following_message(self):
        self.service.receive("a", "telegram", telegram(FULL_REQUEST + ", ko'rsam bo'ladimi?", message=1))
        self.now = 1010
        result, = self.service.receive("a", "telegram", telegram("600$ gacha", message=2, at=1005))
        self.assertNotIn("[L-001]", result.reply)
        self.assertEqual(len(self.stats.sessions("a")), 2)
        self.assertEqual(self.stats.stats("a").handovers, 1)

    def test_inactivity_starts_new_session_without_old_criteria(self):
        self.service.receive("a", "telegram", telegram(message=1))
        self.now = 3002
        result, = self.service.receive("a", "telegram", telegram("600$ gacha", message=2, at=3001))
        self.assertNotIn("[L-001]", result.reply)
        self.assertEqual(len(self.stats.sessions("a")), 2)

    def test_control_uses_configured_admin_not_payload_flags_and_is_tenant_scoped(self):
        result, = self.service.receive("a", "telegram", telegram("L-001 taken", is_control=True))
        self.assertEqual(result.status, "sent")
        self.assertTrue(self.registry.get("a").kb.get("L-001").available)
        result, = self.service.receive("a", "telegram", telegram("L-001 taken", client=900))
        self.assertEqual(result.status, "control")
        self.assertFalse(self.registry.get("a").kb.get("L-001").available)
        self.assertTrue(self.registry.get("b").kb.get("L-001").available)

    def test_authorized_publish_delivers_album_to_channel_once(self):
        payload = telegram("/publish L-007\nYunusobod, 2 xonali, 50 m2, 500$", client=900,
                           photo=[{"file_id": "synthetic_photo", "width": 100, "height": 100}],
                           video={"file_id": "synthetic_video"})
        result, = self.service.receive("a", "telegram", payload)
        self.assertEqual(result.status, "control")
        calls = self.wires["a", "telegram"].calls
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0].endswith("/sendMediaGroup"))
        self.assertEqual(calls[0][1]["chat_id"], "@synthetic_a")
        self.assertEqual([item["type"] for item in calls[0][1]["media"]], ["photo", "video"])
        self.assertEqual(self.service.receive("a", "telegram", payload)[0].status, "duplicate")
        self.assertEqual(len(calls), 2)

    def test_only_configured_channel_posts_ingest_with_post_and_file_reference(self):
        post = {"channel_post": {"message_id": 77, "date": 1000,
                "chat": {"id": -100123456, "type": "channel", "username": "synthetic_a"},
                "caption": "Yunusobod, 2 xonali, 50 m2, 500$",
                "photo": [{"file_id": "synthetic_channel_photo", "width": 100, "height": 100}]}}
        self.assertEqual(self.service.receive("b", "telegram", post), [])
        result, = self.service.receive("a", "telegram", post)
        self.assertEqual(result.status, "ingested")
        added = [item for item in self.registry.get("a").kb.all() if item.id != "L-001"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].post_url, "https://t.me/synthetic_a/77")
        self.assertEqual(added[0].photos, ["synthetic_channel_photo"])
        self.assertEqual(self.service.receive("a", "telegram", post)[0].status, "duplicate")
        self.assertEqual(self.wires["a", "telegram"].calls, [])

    def test_missing_credentials_fails_before_history_or_success(self):
        self.service._connectors.clear()
        with patch.dict(os.environ, {}, clear=True):
            for channel, payload in (("telegram", telegram()), ("instagram", instagram())):
                with self.subTest(channel=channel), self.assertRaises(ConfigurationError):
                    self.service.receive("a", channel, payload)
        self.assertEqual(self.store.scope("a").history("telegram", "111"), [])
        self.assertEqual(self.latency.summary("a")["count"], 0)

    def test_failed_delivery_retries_preserve_first_receipt_then_deduplicate(self):
        wire = self.wires["a", "telegram"]
        wire.fail_next = True
        self.now = 1010
        with self.assertRaises(ConnectorError):
            self.service.receive("a", "telegram", telegram())
        self.assertEqual(self.latency.summary("a")["failed"], 1)
        self.assertEqual(self.latency.summary("a")["count"], 0)
        self.now = 1070
        self.assertEqual(self.service.receive("a", "telegram", telegram(at=1050))[0].status, "sent")
        self.assertEqual(self.latency.summary("a")["p95_seconds"], 70)
        self.assertEqual(self.latency.summary("a")["failed"], 0)
        self.assertEqual(len(self.store.scope("a").history("telegram", "111")), 2)
        self.assertEqual(self.service.receive("a", "telegram", telegram(at=1060))[0].status, "duplicate")
        self.assertEqual(len(wire.calls), 2)

    def test_empty_safety_fallback_is_not_sent_or_counted_as_success(self):
        with patch("estate.service.handle", return_value=("", ["synthetic safety rejection"])):
            result, = self.service.receive("a", "telegram", telegram())
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.wires["a", "telegram"].calls, [])
        self.assertEqual(self.latency.summary("a")["count"], 0)
        self.assertEqual(self.latency.summary("a")["failed"], 1)
        self.assertEqual(self.service.receive("a", "telegram", telegram())[0].status, "duplicate")

    def test_final_checker_prevents_unsafe_reply_before_wire_send(self):
        with patch("estate.service.handle", return_value=("Call PrivateOwnera", [])):
            with self.assertRaises(ValueError):
                self.service.receive("a", "telegram", telegram())
        self.assertEqual(self.wires["a", "telegram"].calls, [])
        self.assertEqual(self.latency.summary("a")["failed"], 1)

    def test_voice_transcriber_and_loader_stubs_feed_normal_conversation_pipeline(self):
        self.service.transcriber = Mock()
        self.service.transcriber.transcribe.return_value = FULL_REQUEST
        self.service.audio_loader = Mock(return_value=b"synthetic_audio_bytes")
        payloads = (("telegram", telegram(None, voice={"file_id": "synthetic_voice"})),
                    ("instagram", instagram(None, attachments=[{"type": "audio", "payload": {
                        "url": "https://scontent.cdninstagram.com/synthetic.ogg"}}])))
        for channel, payload in payloads:
            with self.subTest(channel=channel):
                result, = self.service.receive("a", channel, payload)
                self.assertEqual(result.status, "sent")
                self.assertIn("[L-001]", result.reply)
                self.assertEqual(self.store.scope("a").history(channel, "111")[0]["text"], FULL_REQUEST)
        self.assertEqual(self.service.audio_loader.call_count, 2)
        self.service.transcriber.transcribe.assert_called_with(b"synthetic_audio_bytes", language="auto")

    def test_voice_failure_returns_text_request_without_provider_error_or_url(self):
        self.service.transcriber = Mock()
        self.service.audio_loader = Mock(side_effect=RuntimeError("synthetic_private_signed_url " + TG_TOKEN))
        result, = self.service.receive("a", "telegram", telegram(None, voice={"file_id": "synthetic_voice"}))
        self.assertEqual(result.status, "sent")
        self.assertIn("Please send your request as text", result.reply)
        self.assertNotIn("synthetic_private", result.reply)
        self.assertNotIn(TG_TOKEN, result.reply)
        self.service.transcriber.transcribe.assert_not_called()


class ApplicationDispatchIntegrationTest(ApplicationFixture):
    def test_admin_authentication_required_for_all_data_and_write_routes(self):
        for method, path in (("GET", "/admin/instances"), ("GET", "/admin/instances/a/stats"),
                             ("GET", "/admin/instances/a/sessions"), ("POST", "/admin/instances/a/history"),
                             ("POST", "/admin/instances/a/deals"), ("POST", "/admin/instances/a/control")):
            with self.subTest(path=path):
                self.assertEqual(self.request(method, path, {}, authorized=False).status, 401)
                self.assertEqual(self.request(method, path, {}, headers={"Authorization": "Bearer wrong"}).status, 401)

    def test_non_ascii_invalid_admin_authorization_is_rejected_not_an_uncaught_error(self):
        result = self.request("GET", "/admin/instances", authorized=False,
                              headers={"Authorization": "Bearer synthetic_é_invalid"})
        self.assertEqual(result.status, 401)

    def test_admin_create_accepts_environment_references_but_never_exposes_secrets(self):
        status, value = self.json_request("POST", "/admin/instances", {
            "instance_id": "synthetic-new", "realtor_name": "Synthetic New", "telegram_channel": "@synthetic_new",
            "telegram_token_env": "SYNTHETIC_TG_TOKEN", "telegram_admin_ids": ["999"]})
        self.assertEqual(status, 201)
        self.assertEqual(value["id"], "synthetic-new")
        restored = self.registry.get("synthetic-new")
        self.assertEqual(restored.telegram_token_env, "SYNTHETIC_TG_TOKEN")
        self.assertEqual(restored.telegram_admin_ids, ("999",))
        listing = self.request("GET", "/admin/instances")
        self.assertEqual(listing.status, 200)
        for secret in (TG_TOKEN, IG_TOKEN, WEBHOOK_SECRET, META_SECRET):
            self.assertNotIn(secret.encode(), listing.body)

    def test_admin_create_rejects_secret_values_unknown_fields_and_non_slug_ids(self):
        base = {"instance_id": "synthetic-new", "realtor_name": "Synthetic New", "telegram_channel": "@synthetic_new"}
        for change in ({"telegram_token": TG_TOKEN}, {"telegram_token_env": TG_TOKEN},
                       {"instance_id": "../synthetic"}):
            with self.subTest(change=change):
                result = self.request("POST", "/admin/instances", {**base, **change})
                self.assertEqual(result.status, 400)
                self.assertNotIn(TG_TOKEN.encode(), result.body)
        self.assertIsNone(self.registry.get("synthetic-new"))

    def test_tenant_history_stats_sessions_and_deals_remain_isolated(self):
        self.service.receive("a", "telegram", telegram(message=1))
        self.service.receive("b", "telegram", telegram(message=2))
        status, history = self.json_request("POST", "/admin/instances/a/history", {"channel": "telegram", "client_id": "111"})
        self.assertEqual(status, 200)
        self.assertIn("Tenant a", history[-1]["text"])
        self.assertNotIn("Tenant b", str(history))
        status, sessions = self.json_request("GET", "/admin/instances/a/sessions")
        self.assertEqual(status, 200)
        session_id = sessions[0]["session_id"]
        self.assertEqual(self.request("POST", "/admin/instances/b/deals", {"session_id": session_id}).status, 400)
        for _ in range(2):
            self.assertEqual(self.request("POST", "/admin/instances/a/deals", {"session_id": session_id}).status, 200)
        for tenant, expected in (("a", 1), ("b", 0)):
            status, data = self.json_request("GET", "/admin/instances/" + tenant + "/stats")
            self.assertEqual(status, 200)
            self.assertEqual(data["metrics"]["closed_deals"], expected)
            self.assertEqual(data["latency"]["count"], 1)
        self.assertEqual(self.request("POST", "/admin/instances/missing/history", {}).status, 404)

    def test_admin_control_does_not_modify_other_tenant(self):
        self.assertEqual(self.request("POST", "/admin/instances/a/control", {"text": "L-001 taken"}).status, 200)
        self.assertFalse(self.registry.get("a").kb.get("L-001").available)
        self.assertTrue(self.registry.get("b").kb.get("L-001").available)

    def test_telegram_secret_checked_before_normalization_or_delivery(self):
        body = json.dumps(telegram()).encode()
        for headers in ({}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}):
            self.assertEqual(self.request("POST", "/webhooks/a/telegram", headers=headers, body=body).status, 403)
        self.assertEqual(self.wires["a", "telegram"].calls, [])
        result = self.request("POST", "/webhooks/a/telegram", body=body,
                              headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET})
        self.assertEqual(result.status, 200)
        self.assertEqual(json.loads(result.body)["events"], [{"id": "42", "status": "sent"}])

    def test_meta_hmac_authenticates_original_bytes_before_delivery(self):
        body = json.dumps(instagram(), ensure_ascii=False).encode()
        signature = "sha256=" + hmac.new(META_SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers = {"X-Hub-Signature-256": signature}
        self.assertEqual(self.request("POST", "/webhooks/a/instagram", body=body + b" ", headers=headers).status, 403)
        self.assertEqual(self.request("POST", "/webhooks/a/instagram", body=body).status, 403)
        self.assertEqual(self.wires["a", "instagram"].calls, [])
        self.assertEqual(self.request("POST", "/webhooks/a/instagram", body=body, headers=headers).status, 200)
        self.assertEqual(self.latency.summary("a")["count"], 1)

    def test_meta_subscription_challenge_and_ambiguous_query_validation(self):
        query = {"hub.mode": "subscribe", "hub.verify_token": WEBHOOK_SECRET, "hub.challenge": "synthetic-123"}
        result = self.request("GET", "/webhooks/a/instagram?" + urlencode(query))
        self.assertEqual((result.status, result.body), (200, b"synthetic-123"))
        for change in ({"hub.verify_token": "wrong"}, {"hub.mode": "unsubscribe"}, {"hub.challenge": "x" * 257}):
            self.assertEqual(self.request("GET", "/webhooks/a/instagram?" + urlencode({**query, **change})).status, 403)
        duplicate = urlencode(query) + "&hub.verify_token=" + WEBHOOK_SECRET
        self.assertEqual(self.request("GET", "/webhooks/a/instagram?" + duplicate).status, 403)

    def test_unconfigured_webhook_credentials_are_not_success(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.request("POST", "/webhooks/a/telegram", {}).status, 503)
        with patch.dict(os.environ, {"SYNTHETIC_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True):
            self.assertEqual(self.request("POST", "/webhooks/a/instagram", {}).status, 503)
        self.assertEqual(self.latency.summary("a")["count"], 0)

    def test_invalid_json_and_oversized_bodies_are_rejected_without_send(self):
        headers = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
        for body in (b"{", b"[]", b"null", b"\xff"):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/webhooks/a/telegram", body=body, headers=headers).status, 400)
        self.assertEqual(self.request("POST", "/webhooks/a/telegram", body=b" " * (MAX_BODY + 1)).status, 413)
        self.assertEqual(self.wires["a", "telegram"].calls, [])

    def test_provider_error_is_sanitized_retryable_and_does_not_ack_success(self):
        headers = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
        self.wires["a", "telegram"].fail_next = True
        result = self.request("POST", "/webhooks/a/telegram", telegram(), headers=headers)
        self.assertEqual(result.status, 502)
        self.assertNotIn(TG_TOKEN.encode(), result.body)
        self.assertNotIn(b"synthetic_provider_private", result.body)
        self.assertEqual(self.latency.summary("a")["count"], 0)
        self.assertEqual(self.request("POST", "/webhooks/a/telegram", telegram(), headers=headers).status, 200)
        self.assertEqual(self.latency.summary("a")["count"], 1)

    def test_health_does_not_claim_live_deployment_verification(self):
        status, value = self.json_request("GET", "/health", authorized=False)
        self.assertEqual(status, 200)
        self.assertFalse(value["deployment_verified"])


if __name__ == "__main__":
    unittest.main()
