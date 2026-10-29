"""Offline wire-contract tests. All credentials below are synthetic test data."""
import dataclasses
import hashlib
import hmac
import json
import traceback
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request

from estate.connectors import (
    Attachment, ConnectorError, IncomingMessage, InstagramConnector,
    TelegramConnector, verify_meta_subscription,
)
from estate import connectors


TG_TOKEN = "123456:offline_test_only"
IG_TOKEN = "offline_test_only"
ACCOUNT = "17841400000123456"
CUSTOMER = "987654321001234"
PHOTO = "https://cdn.example.com/rental.jpg"
VIDEO = "https://cdn.example.com/rental.mp4"


def telegram_message(**changes):
    message = {"message_id": 42, "date": 1700000000,
               "chat": {"id": 1234567890123, "type": "private"},
               "from": {"id": 1234567890123, "is_bot": False}, "text": "Здравствуйте"}
    message.update(changes)
    return {"update_id": 100, "message": message}


def telegram_post(**changes):
    message = {"message_id": 73, "date": 1700000001,
               "chat": {"id": -1001234567890, "type": "channel", "username": "Estate_Demo"},
               "text": "Yunusobod, 2 xonali, 550$"}
    message.update(changes)
    return {"update_id": 101, "channel_post": message}


def instagram_event(**changes):
    event = {"sender": {"id": CUSTOMER}, "recipient": {"id": ACCOUNT},
             "timestamp": 1700000000123, "message": {"mid": "aWdfZAG1fMQ", "text": "Hello"}}
    event.update(changes)
    return event


def instagram_update(*events, account=ACCOUNT):
    return {"object": "instagram", "entry": [{"id": account, "time": 1700000001000,
                                               "messaging": list(events)}]}


class FakeTransport:
    """Capture actual Request bytes/headers, return official-shaped API responses."""
    def __init__(self, *responses):
        self.calls = []
        self.responses = list(responses)

    def __call__(self, request, *, timeout):
        payload = json.loads(request.data)
        self.calls.append({"url": request.full_url, "method": request.get_method(),
                           "headers": {k.lower(): v for k, v in request.header_items()},
                           "body": payload, "timeout": timeout})
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        if request.full_url.startswith("https://api.telegram.org/"):
            result = {"message_id": 201, "date": 1700000010,
                      "chat": {"id": payload["chat_id"], "type": "private"}}
            if request.full_url.endswith("/sendMediaGroup"):
                result = [{**result, "message_id": 201 + i} for i in range(len(payload["media"]))]
            return 200, json.dumps({"ok": True, "result": result}).encode()
        return 200, json.dumps({"recipient_id": payload["recipient"]["id"],
                                "message_id": "aWdfb3V0XzE"}).encode()


class ContractTest(unittest.TestCase):
    def test_frozen_exact_contract_and_defaults(self):
        self.assertEqual([f.name for f in dataclasses.fields(Attachment)], ["kind", "url"])
        self.assertEqual([f.name for f in dataclasses.fields(IncomingMessage)],
                         ["id", "client_id", "text", "timestamp", "attachments", "is_control", "is_listing", "post_url"])
        message = IncomingMessage("m", "u", "t")
        self.assertEqual(message, IncomingMessage("m", "u", "t", None, (), False, False, ""))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            message.text = "changed"
        attachment = Attachment("photo", PHOTO)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            attachment.url = VIDEO
        self.assertIsInstance(hash(message), int)

    def test_connectors_repr_does_not_show_credentials(self):
        for connector in (TelegramConnector(TG_TOKEN), InstagramConnector(ACCOUNT, IG_TOKEN)):
            self.assertNotIn(TG_TOKEN, repr(connector))
            self.assertNotIn(IG_TOKEN, repr(connector))

    def test_parse_does_not_require_credentials_or_use_transport(self):
        def forbidden(*args, **kwargs):
            self.fail("Parsing must not make an HTTP request")
        self.assertEqual(TelegramConnector("", transport=forbidden).parse_update(telegram_message())[0].text,
                         "Здравствуйте")
        self.assertEqual(InstagramConnector(ACCOUNT, "", transport=forbidden).parse_update(
            instagram_update(instagram_event()))[0].text, "Hello")


class TelegramParsingTest(unittest.TestCase):
    def setUp(self):
        self.connector = TelegramConnector("", channel="@estate_demo", transport=FakeTransport())

    def test_private_text_ids_timestamp_and_untrusted_flags(self):
        message = self.connector.parse_update(telegram_message(is_control=True, is_listing=True))[0]
        self.assertEqual(message, IncomingMessage("42", "1234567890123", "Здравствуйте", 1700000000.0))
        self.assertIsInstance(message.id, str)
        self.assertIsInstance(message.client_id, str)
        self.assertIsInstance(message.timestamp, float)

    def test_photo_uses_largest_dimensions_not_thumbnail_or_unique_id(self):
        payload = telegram_message(text=None, caption="2 xonali", photo=[
            {"file_id": "small_file", "file_unique_id": "not_reusable", "width": 90, "height": 60},
            {"file_id": "largest-file", "file_unique_id": "not_reusable", "width": 1280, "height": 720},
            {"file_id": "medium_file", "width": 640, "height": 480},
        ])
        message = self.connector.parse_update(payload)[0]
        self.assertEqual(message.text, "2 xonali")
        self.assertEqual(message.attachments, (Attachment("photo", "largest-file"),))

    def test_video_and_voice_metadata_without_network_resolution(self):
        for kind in ("video", "voice"):
            with self.subTest(kind=kind):
                payload = telegram_message(text=None, **{kind: {
                    "file_id": "AgAC_test-file_123", "file_unique_id": "unique",
                    "duration": 3, "mime_type": "video/mp4" if kind == "video" else "audio/ogg"}})
                message = self.connector.parse_update(payload)[0]
                self.assertEqual(message.text, "")
                self.assertEqual(message.attachments, (Attachment(kind, "AgAC_test-file_123"),))

    def test_unsupported_service_messages_and_unsafe_file_ids_are_not_emitted(self):
        for changes in ({"text": None, "new_chat_members": [{"id": 2}]},
                        {"text": None, "document": {"file_id": "file_id"}},
                        {"text": None, "photo": [{"file_id": "https://127.0.0.1/a"}]},
                        {"text": None, "video": {"file_id": "../../secret"}}):
            with self.subTest(changes=changes):
                self.assertEqual(self.connector.parse_update(telegram_message(**changes)), [])

    def test_groups_echoes_outbound_and_missing_sender_ignored(self):
        for changes in (
            {"chat": {"id": 1234567890123, "type": "group"}},
            {"chat": {"id": 1234567890123, "type": "supergroup"}},
            {"from": {"id": 1234567890123, "is_bot": True}},
            {"from": {"id": 999, "is_bot": True}},
            {"from": {"id": 999}}, {"from": None},
            {"sender_chat": {"id": -10012345}},
            {"is_echo": True}, {"outgoing": True}, {"is_outgoing": True},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.connector.parse_update(telegram_message(**changes)), [])

    def test_channel_username_is_case_insensitive_and_listing_not_control(self):
        message = self.connector.parse_update(telegram_post(is_control=True))[0]
        self.assertEqual(message.client_id, "-1001234567890")
        self.assertEqual(message.id, "73")
        self.assertTrue(message.is_listing)
        self.assertFalse(message.is_control)
        self.assertEqual(message.post_url, "https://t.me/Estate_Demo/73")

    def test_channel_numeric_match_does_not_require_username(self):
        connector = TelegramConnector("", "-1001234567890")
        message = connector.parse_update(telegram_post(chat={"id": -1001234567890, "type": "channel"}))[0]
        self.assertEqual(message.post_url, "https://t.me/c/1234567890/73")
        self.assertTrue(message.is_listing)
        # Numeric configuration must match the ID, not a coincidental username.
        self.assertEqual(connector.parse_update(telegram_post(chat={
            "id": -1009999999999, "type": "channel", "username": "Estate_Demo"})), [])

    def test_unconfigured_and_unrelated_channels_not_emitted(self):
        for channel in ("", "@another_demo", "-1009999999999", "https://t.me/Estate_Demo", "@Estate_Demo/evil"):
            with self.subTest(channel=channel):
                self.assertEqual(TelegramConnector("", channel).parse_update(telegram_post()), [])
        self.assertEqual(self.connector.parse_update(telegram_post(chat={
            "id": -1001234567890, "type": "group", "username": "Estate_Demo"})), [])
        self.assertEqual(self.connector.parse_update(telegram_post(chat={
            "id": -1001234567890, "type": "channel", "username": "estate_demo.evil"})), [])

    def test_channel_photo_caption_and_video_metadata(self):
        for kind, field in (("photo", [{"file_id": "channel_photo", "width": 100, "height": 200}]),
                            ("video", {"file_id": "channel_video"})):
            message = self.connector.parse_update(telegram_post(text=None, caption="Listing", **{kind: field}))[0]
            self.assertEqual(message.text, "Listing")
            self.assertEqual(message.attachments, (Attachment(kind, "channel_" + kind),))
            self.assertTrue(message.is_listing)

    def test_edits_and_other_update_types_are_ignored(self):
        for field in ("edited_message", "edited_channel_post", "business_message", "callback_query", "my_chat_member"):
            with self.subTest(field=field):
                self.assertEqual(self.connector.parse_update({field: telegram_message()["message"]}), [])

    def test_malformed_payloads_fail_closed_without_exceptions(self):
        for payload in (None, [], {}, {"message": None}, {"message": []},
                        telegram_message(chat=None), telegram_message(message_id=None),
                        telegram_message(photo=[None, 1, {"file_id": []}]),
                        telegram_message(video=[]), telegram_message(text=[], caption={})):
            with self.subTest(payload=payload):
                result = self.connector.parse_update(payload)
                self.assertIsInstance(result, list)
        self.assertEqual(self.connector.parse_update(telegram_message(message_id=None)), [])

    def test_timestamp_is_optional_finite_and_not_guessed(self):
        for value in (None, "1700000000", True, float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(value_type=type(value)):
                message = self.connector.parse_update(telegram_message(date=value))[0]
                self.assertIsNone(message.timestamp)


class InstagramParsingTest(unittest.TestCase):
    def setUp(self):
        self.connector = InstagramConnector(ACCOUNT, "", transport=FakeTransport())

    def test_private_text_and_millisecond_timestamp(self):
        message = self.connector.parse_update(instagram_update(instagram_event()))[0]
        self.assertEqual(message, IncomingMessage("aWdfZAG1fMQ", CUSTOMER, "Hello", 1700000000.123))

    def test_images_videos_and_audio_url_metadata(self):
        payload = instagram_update(instagram_event(message={"mid": "m-2", "text": "Here", "attachments": [
            {"type": "image", "payload": {"url": PHOTO}},
            {"type": "video", "payload": {"url": VIDEO}},
            {"type": "audio", "payload": {"url": "https://cdn.example.com/note.ogg"}},
        ]}))
        message = self.connector.parse_update(payload)[0]
        self.assertEqual(message.attachments, (Attachment("photo", PHOTO), Attachment("video", VIDEO),
                                              Attachment("voice", "https://cdn.example.com/note.ogg")))

    def test_media_only_incoming_message(self):
        message = self.connector.parse_update(instagram_update(instagram_event(message={
            "mid": "m-3", "attachments": [{"type": "image", "payload": {"url": PHOTO}}]})))[0]
        self.assertEqual((message.text, message.attachments), ("", (Attachment("photo", PHOTO),)))

    def test_wrong_object_account_recipient_and_outbound_suppression(self):
        updates = [
            {"object": "page", "entry": instagram_update(instagram_event())["entry"]},
            instagram_update(instagram_event(), account="other_account"),
            instagram_update(instagram_event(recipient={"id": "other_account"})),
            instagram_update(instagram_event(sender={"id": ACCOUNT}, recipient={"id": CUSTOMER})),
            instagram_update(instagram_event(sender={"id": ACCOUNT})),
            instagram_update(instagram_event(message={"mid": "echo", "text": "out", "is_echo": True})),
            instagram_update(instagram_event(is_echo=True)),
            instagram_update(instagram_event(is_group=True)),
            instagram_update(instagram_event(message={"mid": "group", "text": "out", "is_group": True})),
            instagram_update(instagram_event(message={"mid": "deleted", "text": "out", "is_deleted": True})),
        ]
        for update in updates:
            with self.subTest(update=update):
                self.assertEqual(self.connector.parse_update(update), [])
        self.assertEqual(InstagramConnector("", "").parse_update(instagram_update(instagram_event())), [])

    def test_batched_events_filter_individually_and_keep_order(self):
        payload = instagram_update(instagram_event(), instagram_event(recipient={"id": "other"}),
                                   instagram_event(message={"mid": "second", "text": "Two"}))
        payload["entry"].insert(0, {"id": "other", "messaging": [instagram_event()]})
        self.assertEqual([m.id for m in self.connector.parse_update(payload)], ["aWdfZAG1fMQ", "second"])

    def test_user_payload_cannot_set_control_or_listing(self):
        message = self.connector.parse_update(instagram_update(instagram_event(
            is_control=True, is_listing=True, message={"mid": "flags", "text": "taken", "is_control": True})))[0]
        self.assertFalse(message.is_control)
        self.assertFalse(message.is_listing)
        self.assertEqual(message.post_url, "")

    def test_unsupported_events_and_unsafe_media_are_ignored(self):
        events = [
            instagram_event(message=None, read={"mid": "read"}),
            instagram_event(message=None, delivery={"mids": ["one"]}),
            instagram_event(message={"text": "No id"}),
            instagram_event(message={"mid": "no-content", "attachments": [
                {"type": "share", "payload": {"url": PHOTO}},
                {"type": "image", "payload": {"url": "http://cdn.example.com/a.jpg"}},
                {"type": "image", "payload": {"url": "https://127.0.0.1/a.jpg"}}]}),
        ]
        for event in events:
            self.assertEqual(self.connector.parse_update(instagram_update(event)), [])

    def test_malformed_containers_and_missing_timestamp(self):
        for payload in (None, [], {}, {"object": "instagram", "entry": None},
                        {"object": "instagram", "entry": [None, [], {"id": ACCOUNT, "messaging": None}]},
                        instagram_update(None, [], instagram_event(sender=None)),
                        instagram_update(instagram_event(recipient=[{"id": ACCOUNT}]))):
            with self.subTest(payload=payload):
                self.assertEqual(self.connector.parse_update(payload), [])
        for timestamp in (None, "1700000000123", False, float("inf")):
            self.assertIsNone(self.connector.parse_update(instagram_update(instagram_event(timestamp=timestamp)))[0].timestamp)


class TelegramSendingTest(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.connector = TelegramConnector(TG_TOKEN, "@estate_demo", self.transport)

    def test_text_exact_endpoint_headers_body_timeout_and_none_return(self):
        self.assertIsNone(self.connector.send("12345", "Привет"))
        self.assertEqual(self.transport.calls, [{
            "url": f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", "method": "POST",
            "headers": {"content-type": "application/json"},
            "body": {"chat_id": "12345", "text": "Привет"}, "timeout": 10.0}])

    def test_actual_photo_or_video_method_not_text_url(self):
        for kind, method, url in (("photo", "sendPhoto", PHOTO), ("video", "sendVideo", VIDEO)):
            with self.subTest(kind=kind):
                self.connector.send("12345", "Listing", (Attachment(kind, url),))
                call = self.transport.calls[-1]
                self.assertEqual(call["url"], f"https://api.telegram.org/bot{TG_TOKEN}/{method}")
                self.assertEqual(call["body"], {"chat_id": "12345", kind: url, "caption": "Listing"})

    def test_reuses_strict_telegram_file_ids(self):
        self.connector.send("12345", "", (Attachment("photo", "AgAC-file_id_123"),))
        self.assertEqual(self.transport.calls[0]["body"], {"chat_id": "12345", "photo": "AgAC-file_id_123"})

    def test_mixed_photo_video_album_exact_wire_shape(self):
        self.connector.send("12345", "Album", (Attachment("photo", PHOTO), Attachment("video", VIDEO)))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0]["url"], f"https://api.telegram.org/bot{TG_TOKEN}/sendMediaGroup")
        self.assertEqual(self.transport.calls[0]["body"], {"chat_id": "12345", "media": [
            {"type": "photo", "media": PHOTO, "caption": "Album"}, {"type": "video", "media": VIDEO}]})

    def test_long_caption_sent_as_separate_text_without_truncation(self):
        text = "я" * 1025
        self.connector.send("12345", text, (Attachment("photo", PHOTO),))
        self.assertEqual([c["url"].rsplit("/", 1)[1] for c in self.transport.calls], ["sendMessage", "sendPhoto"])
        self.assertEqual(self.transport.calls[0]["body"]["text"], text)
        self.assertNotIn("caption", self.transport.calls[1]["body"])

    def test_caption_limit_boundary(self):
        self.connector.send("12345", "x" * 1024, (Attachment("video", VIDEO),))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(len(self.transport.calls[0]["body"]["caption"]), 1024)

    def test_publish_text_returns_message(self):
        result = self.connector.publish("New listing")
        self.assertEqual(result["message_id"], 201)
        self.assertEqual(self.transport.calls[0]["body"], {"chat_id": "@estate_demo", "text": "New listing"})

    def test_publish_single_media_and_album_return_shapes(self):
        for kind, url in (("photo", PHOTO), ("video", VIDEO)):
            self.assertIsInstance(self.connector.publish("Listing", (Attachment(kind, url),)), dict)
        result = self.connector.publish("Listing", (Attachment("photo", PHOTO), Attachment("video", VIDEO)))
        self.assertEqual([m["message_id"] for m in result], [201, 202])
        self.assertEqual(self.transport.calls[-1]["body"]["chat_id"], "@estate_demo")

    def test_publish_long_text_and_media_returns_all_messages(self):
        result = self.connector.publish("x" * 1025, (Attachment("photo", PHOTO), Attachment("video", VIDEO)))
        self.assertEqual(len(result), 3)
        self.assertEqual([m["message_id"] for m in result], [201, 201, 202])

    def test_publish_numeric_channel_id(self):
        connector = TelegramConnector(TG_TOKEN, "-1001234567890", self.transport)
        connector.publish("Listing", (Attachment("video", VIDEO),))
        self.assertEqual(self.transport.calls[0]["body"]["chat_id"], "-1001234567890")

    def test_publish_requires_configured_valid_channel(self):
        for channel in ("", "https://t.me/estate_demo", "@estate_demo/evil", "@estate_demo\n"):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                TelegramConnector(TG_TOKEN, channel, self.transport).publish("Listing")
        self.assertEqual(self.transport.calls, [])

    def test_invalid_send_content_rejected_before_any_http_call(self):
        cases = [("", ()), ("x" * 4097, ()), ("Caption", (Attachment("voice", "file_id"),)),
                 ("Caption", (Attachment("photo", "file_id"),) * 11),
                 ("Caption", (Attachment([], PHOTO),)),
                 ("Text first", (Attachment("photo", PHOTO), Attachment("video", "http://bad.example.com/a"))),
                 ("Text", [Attachment("photo", PHOTO)]), (None, ())]
        for text, attachments in cases:
            with self.subTest(text_type=type(text), count=len(attachments)), self.assertRaises(ValueError):
                self.connector.send("12345", text, attachments)
        self.assertEqual(self.transport.calls, [])

    def test_maximum_album_and_text_limits(self):
        self.connector.send("12345", "x" * 4096, (Attachment("photo", "file_id"),) * 10)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(len(self.transport.calls[1]["body"]["media"]), 10)

    def test_missing_or_injected_token_rejected_only_at_send(self):
        for token in ("", "not_a_bot_token", "123:secret/evil", "123:secret?x=y", "123:secret\n"):
            connector = TelegramConnector(token, transport=self.transport)
            self.assertEqual(len(connector.parse_update(telegram_message())), 1)
            with self.assertRaises(ValueError) as caught:
                connector.send("12345", "Hi")
            if token:
                self.assertNotIn(token, str(caught.exception))
        self.assertEqual(self.transport.calls, [])


class InstagramSendingTest(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.connector = InstagramConnector(ACCOUNT, IG_TOKEN, transport=self.transport)

    def test_exact_text_endpoint_bearer_header_and_body(self):
        self.assertIsNone(self.connector.send(CUSTOMER, "Hello"))
        self.assertEqual(self.transport.calls, [{
            "url": f"https://graph.instagram.com/v24.0/{ACCOUNT}/messages", "method": "POST",
            "headers": {"content-type": "application/json", "authorization": f"Bearer {IG_TOKEN}"},
            "body": {"recipient": {"id": CUSTOMER}, "message": {"text": "Hello"}}, "timeout": 10.0}])
        self.assertNotIn("access_token", self.transport.calls[0]["url"])
        self.assertNotIn("access_token", self.transport.calls[0]["body"])

    def test_actual_single_image_and_video_attachment_shapes(self):
        self.connector.send(CUSTOMER, "Listing", (Attachment("photo", PHOTO), Attachment("video", VIDEO)))
        self.assertEqual([c["body"] for c in self.transport.calls], [
            {"recipient": {"id": CUSTOMER}, "message": {"text": "Listing"}},
            {"recipient": {"id": CUSTOMER}, "message": {
                "attachment": {"type": "image", "payload": {"url": PHOTO}}}},
            {"recipient": {"id": CUSTOMER}, "message": {
                "attachment": {"type": "video", "payload": {"url": VIDEO}}}},
        ])

    def test_media_only_does_not_send_empty_text(self):
        self.connector.send(CUSTOMER, "", (Attachment("video", VIDEO),))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertNotIn("text", self.transport.calls[0]["body"]["message"])

    def test_api_version_is_configurable_but_not_an_origin_or_path(self):
        InstagramConnector(ACCOUNT, IG_TOKEN, "v25.0", self.transport).send(CUSTOMER, "Hi")
        self.assertEqual(self.transport.calls[0]["url"], f"https://graph.instagram.com/v25.0/{ACCOUNT}/messages")
        for version in ("https://evil.example.com", "v24.0/../../", "v24.0?access_token=", "v24.0\n", ""):
            with self.subTest(version=version), self.assertRaises(ValueError):
                InstagramConnector(ACCOUNT, IG_TOKEN, version, self.transport).send(CUSTOMER, "Hi")
        self.assertEqual(len(self.transport.calls), 1)

    def test_missing_account_or_credentials_and_header_injection_rejected_on_send(self):
        for account, token in ((ACCOUNT, ""), ("", IG_TOKEN), ("me/evil", IG_TOKEN),
                               ("@profile", IG_TOKEN), (ACCOUNT, "secret\r\nX-Injected: yes")):
            connector = InstagramConnector(account, token, transport=self.transport)
            connector.parse_update(instagram_update(instagram_event()))
            with self.subTest(account=account), self.assertRaises(ValueError) as caught:
                connector.send(CUSTOMER, "Hi")
            if token:
                self.assertNotIn(token, str(caught.exception))
        self.assertEqual(self.transport.calls, [])

    def test_all_parts_validated_before_sending_anything(self):
        for attachments in ((Attachment("photo", "telegram_file_id"),), (Attachment("document", PHOTO),),
                            (Attachment("photo", PHOTO), Attachment("video", "https://localhost/a"))):
            with self.assertRaises(ValueError):
                self.connector.send(CUSTOMER, "Text would otherwise send first", attachments)
        self.assertEqual(self.transport.calls, [])

    def test_invalid_recipient_and_empty_content(self):
        for client_id in ("", "@customer", "123/evil", "123\n"):
            with self.assertRaises(ValueError):
                self.connector.send(client_id, "Hi")
        with self.assertRaises(ValueError):
            self.connector.send(CUSTOMER, "")
        self.assertEqual(self.transport.calls, [])


class WebhookAuthenticationTest(unittest.TestCase):
    def test_telegram_secret_header_case_insensitive_and_nonempty(self):
        headers = {"x-telegram-bot-api-secret-token": "offline_secret-1"}
        self.assertTrue(TelegramConnector.verify_webhook(headers, "offline_secret-1"))
        for header, secret in (({}, "offline_secret-1"), (headers, "other"), ({}, ""),
                               ({"X-Telegram-Bot-Api-Secret-Token": ""}, ""), (headers, "bad secret")):
            self.assertFalse(TelegramConnector.verify_webhook(header, secret))

    def test_telegram_ambiguous_header_rejected(self):
        headers = {"X-Telegram-Bot-Api-Secret-Token": "secret", "x-telegram-bot-api-secret-token": "secret"}
        self.assertFalse(TelegramConnector.verify_webhook(headers, "secret"))
        self.assertFalse(TelegramConnector.verify_webhook(
            {"X-Telegram-Bot-Api-Secret-Token": "\ud800"}, "secret"))

    def test_channelpost_authentication_before_ingestion(self):
        connector = TelegramConnector("", "@estate_demo")
        body = json.dumps(telegram_post()).encode()
        headers = {"X-Telegram-Bot-Api-Secret-Token": "offline_secret"}
        self.assertTrue(connector.parse_webhook(body, headers, "offline_secret")[0].is_listing)
        for invalid in ({}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}):
            with self.assertRaisesRegex(ConnectorError, "authentication failed"):
                connector.parse_webhook(body, invalid, "offline_secret")
        self.assertEqual(connector.parse_webhook(body, headers, "offline_secret"), connector.parse_update(telegram_post()))

    def test_telegram_no_payload_secret_substitution(self):
        body = json.dumps({**telegram_post(), "secret_token": "offline_secret"}).encode()
        with self.assertRaises(ConnectorError):
            TelegramConnector("", "@estate_demo").parse_webhook(body, {}, "offline_secret")

    def test_meta_signature_exact_raw_bytes(self):
        body = json.dumps(instagram_update(instagram_event()), ensure_ascii=False).encode()
        digest = hmac.new(b"offline_app_secret", body, hashlib.sha256).hexdigest()
        headers = {"x-hub-signature-256": "sha256=" + digest}
        connector = InstagramConnector(ACCOUNT, "")
        self.assertTrue(connector.verify_webhook(body, headers, "offline_app_secret"))
        self.assertEqual(len(connector.parse_webhook(body, headers, "offline_app_secret")), 1)
        self.assertFalse(connector.verify_webhook(body + b" ", headers, "offline_app_secret"))
        self.assertFalse(connector.verify_webhook(body, headers, "wrong_secret"))
        self.assertFalse(connector.verify_webhook(body, headers, ""))
        self.assertFalse(connector.verify_webhook(body.decode(), headers, "offline_app_secret"))
        with self.assertRaisesRegex(ConnectorError, "authentication failed"):
            connector.parse_webhook(body + b" ", headers, "offline_app_secret")

    def test_meta_missing_or_malformed_signature(self):
        for signature in ("", "sha1=" + "a" * 40, "sha256=" + "z" * 64, "a" * 64, "sha256=abc"):
            self.assertFalse(InstagramConnector.verify_webhook(b"{}", {"X-Hub-Signature-256": signature}, "secret"))

    def test_authenticated_but_invalid_json_has_sanitized_error(self):
        for body in (b"RAW_PRIVATE_BODY", b"[]", b"\xff", b"{" + b" " * connectors._MAX_BODY):
            with self.subTest(size=len(body)), self.assertRaises(ConnectorError) as caught:
                TelegramConnector("").parse_webhook(body, {"X-Telegram-Bot-Api-Secret-Token": "secret"}, "secret")
            self.assertEqual(str(caught.exception), "Invalid webhook payload")
            digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
            with self.assertRaisesRegex(ConnectorError, "Invalid webhook payload"):
                InstagramConnector(ACCOUNT, "").parse_webhook(body, {"X-Hub-Signature-256": "sha256=" + digest}, "secret")

    def test_meta_subscription_challenge_requires_mode_and_verify_token(self):
        query = {"hub.mode": "subscribe", "hub.verify_token": "offline_verify_token", "hub.challenge": "12345"}
        self.assertEqual(verify_meta_subscription(query, "offline_verify_token"), "12345")
        self.assertIsNone(verify_meta_subscription(query, "wrong"))
        self.assertIsNone(verify_meta_subscription(query, ""))
        self.assertIsNone(verify_meta_subscription({**query, "hub.mode": "unsubscribe"}, "offline_verify_token"))
        self.assertIsNone(verify_meta_subscription({**query, "hub.challenge": None}, "offline_verify_token"))


class TransportSecurityTest(unittest.TestCase):
    def test_no_media_url_fetch_or_dns_lookup(self):
        transport = FakeTransport()
        with patch("socket.getaddrinfo", side_effect=AssertionError("No local media resolution")):
            TelegramConnector(TG_TOKEN, transport=transport).send("12345", "", (Attachment("photo", PHOTO),))
            InstagramConnector(ACCOUNT, IG_TOKEN, transport=transport).send(CUSTOMER, "", (Attachment("video", VIDEO),))
        self.assertEqual({c["url"].split("/")[2] for c in transport.calls}, {"api.telegram.org", "graph.instagram.com"})

    def test_unsafe_media_urls_rejected_for_both_platforms(self):
        urls = ["http://cdn.example.com/a", "file:///etc/passwd", "ftp://cdn.example.com/a", "//cdn.example.com/a",
                "https://localhost/a", "https://api.localhost/a", "https://metadata.google.internal/a",
                "https://127.0.0.1/a", "https://10.0.0.1/a", "https://169.254.169.254/a", "https://192.168.1.1/a",
                "https://0.0.0.0/a", "https://224.0.0.1/a", "https://[::1]/a", "https://[fe80::1]/a",
                "https://[::ffff:127.0.0.1]/a", "https://2130706433/a", "https://127.1/a", "https://0177.0.0.1/a",
                "https://0x7f000001/a", "https://0x7f.0.0.1/a", "https://%31%32%37.0.0.1/a",
                "https://user:password@cdn.example.com/a", "https://cdn.example.com:8443/a", "https://cdn.example.com:bad/a",
                "https://cdn.example.com/a\nX: secret", "https://cdn.example.com\\@127.0.0.1/a", "https://[broken/a"]
        for url in urls:
            for platform in ("telegram", "instagram"):
                with self.subTest(url=url, platform=platform):
                    transport = FakeTransport()
                    connector = (TelegramConnector(TG_TOKEN, transport=transport) if platform == "telegram"
                                 else InstagramConnector(ACCOUNT, IG_TOKEN, transport=transport))
                    with self.assertRaises(ValueError):
                        connector.send("12345", "", (Attachment("photo", url),))
                    self.assertEqual(transport.calls, [])

    def test_default_transport_installs_redirect_and_proxy_blocks_and_timeout(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"ok":true}'
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(connectors, "build_opener", return_value=opener) as build:
            request = Request("https://api.telegram.org/")
            self.assertEqual(connectors._http(request, timeout=10.0), (200, b'{"ok":true}'))
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(h, ProxyHandler) and h.proxies == {} for h in handlers))
        redirect = next(h for h in handlers if isinstance(h, connectors._NoRedirect))
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(redirect.redirect_request(request, None, code, "Redirect", {}, "https://evil.example.com"))
        opener.open.assert_called_once_with(request, timeout=10.0)
        response.read.assert_called_once_with(connectors._MAX_BODY + 1)

    def test_default_http_error_does_not_return_or_read_body(self):
        body = MagicMock()
        error = HTTPError(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 302, "redirect", {}, body)
        opener = MagicMock()
        opener.open.side_effect = error
        with patch.object(connectors, "build_opener", return_value=opener):
            self.assertEqual(connectors._http(Request("https://api.telegram.org/"), timeout=10), (302, b""))
        body.read.assert_not_called()
        body.close.assert_called_once()

    def test_http_statuses_invalid_bodies_and_errors_are_sanitized(self):
        private = f"RAW_PRIVATE_SERVER_BODY {TG_TOKEN} {IG_TOKEN}"
        responses = [
            (401, private.encode()), (429, private.encode()), (500, private.encode()),
            (302, private.encode()), (307, private.encode()), (200, private.encode()),
            (200, b"[]"), (200, b"null"), (200, b"\xff"), (200, b"x" * (connectors._MAX_BODY + 1)),
            (200, "not bytes"), ("200", b"{}"),
            URLError(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage {private}"),
            TimeoutError(private), OSError(private),
        ]
        for response in responses:
            for platform in ("Telegram", "Instagram"):
                with self.subTest(response_type=type(response), platform=platform):
                    transport = FakeTransport(response)
                    connector = (TelegramConnector(TG_TOKEN, transport=transport) if platform == "Telegram"
                                 else InstagramConnector(ACCOUNT, IG_TOKEN, transport=transport))
                    with self.assertRaises(ConnectorError) as caught:
                        connector.send("12345", "Hi")
                    error = caught.exception
                    self.assertEqual(str(error), f"{platform} request failed")
                    formatted = "".join(traceback.format_exception(type(error), error, error.__traceback__))
                    for secret in (TG_TOKEN, IG_TOKEN, "RAW_PRIVATE_SERVER_BODY"):
                        self.assertNotIn(secret, repr(error))
                        self.assertNotIn(secret, formatted)
                    self.assertEqual(len(transport.calls), 1)

    def test_official_telegram_api_error_shapes(self):
        errors = [
            {"ok": False, "error_code": 401, "description": "Unauthorized private body"},
            {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 30}},
            {"ok": False, "error_code": 400, "description": "Bad Request", "parameters": {"migrate_to_chat_id": -1009999999999}},
            {"ok": True, "result": "invalid"}, {"result": {}}, {"ok": 1, "result": {}},
        ]
        for result in errors:
            transport = FakeTransport((200, json.dumps(result).encode()))
            with self.assertRaisesRegex(ConnectorError, "^Telegram API request failed$"):
                TelegramConnector(TG_TOKEN, transport=transport).send("12345", "Hi")
            self.assertEqual(len(transport.calls), 1)

    def test_invalid_album_response_is_not_returned_as_success(self):
        for result in ({}, [], ["bad"]):
            transport = FakeTransport((200, json.dumps({"ok": True, "result": result}).encode()))
            with self.assertRaises(ConnectorError):
                TelegramConnector(TG_TOKEN, "@estate_demo", transport).publish(
                    "Album", (Attachment("photo", PHOTO), Attachment("video", VIDEO)))

    def test_official_meta_error_and_success_shapes(self):
        error = {"error": {"message": "RAW_PRIVATE_SERVER_BODY", "type": "OAuthException", "code": 190,
                           "error_subcode": 463, "fbtrace_id": "private_trace"}}
        for result in (error, {"recipient_id": CUSTOMER}, {"message_id": None}, {"message_id": ""}):
            transport = FakeTransport((200, json.dumps(result).encode()))
            with self.assertRaisesRegex(ConnectorError, "^Instagram API request failed$"):
                InstagramConnector(ACCOUNT, IG_TOKEN, transport=transport).send(CUSTOMER, "Hi")
            self.assertEqual(len(transport.calls), 1)

    def test_partial_send_stops_without_retrying_or_sending_later_parts(self):
        transport = FakeTransport((200, json.dumps({"recipient_id": CUSTOMER, "message_id": "first"}).encode()),
                                  (400, b'{"error":{"message":"bad media","code":100}}'))
        with self.assertRaises(ConnectorError):
            InstagramConnector(ACCOUNT, IG_TOKEN, transport=transport).send(
                CUSTOMER, "Text", (Attachment("photo", PHOTO), Attachment("video", VIDEO)))
        self.assertEqual(len(transport.calls), 2)


if __name__ == "__main__":
    unittest.main()
