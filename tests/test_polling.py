"""Offline tests for the laptop demo bot. Tokens and IDs are synthetic test data."""
import io
import json
import os
import ssl
import tempfile
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.error import URLError

from estate.connectors import ConnectorError, TelegramConnector
from estate.polling import build_service, load_env_file, main, poll_once, single_copy

TG_TOKEN = "900002:synthetic_polling_test_only"
REALTOR = 700
CLIENT = 111
CONTACT = "@synthetic_demo_realtor"


def message(text, *, sender=CLIENT, message_id=1, **extra):
    value = {"message_id": message_id, "date": 1000, "from": {"id": sender, "is_bot": False},
             "chat": {"id": sender, "type": "private"}, "text": text}
    value.update(extra)
    return {"update_id": message_id, "message": value}


class FakeTelegram:
    """Serve queued getUpdates batches and record every other Bot API call."""

    def __init__(self):
        self.batches, self.calls, self.fail = [], [], set()

    def __call__(self, request, *, timeout):
        method = request.full_url.rsplit("/", 1)[1]
        body = json.loads(request.data)
        self.calls.append((method, body, timeout))
        if method in self.fail:
            return 500, b""
        if method == "getUpdates":
            result = self.batches.pop(0) if self.batches else []
        elif method == "getMe":
            result = {"id": 900002, "is_bot": True, "username": "synthetic_demo_bot"}
        else:
            result = {"message_id": len(self.calls), "chat": {"id": body["chat_id"]}}
        if method == "sendMediaGroup":
            result = [result for _ in body["media"]]
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def sent(self):
        return [(method, body) for method, body, _ in self.calls if method.startswith("send")]


class ConnectorPollingTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket", side_effect=AssertionError("live network forbidden")).start()
        self.wire = FakeTelegram()
        self.connector = TelegramConnector(TG_TOKEN, transport=self.wire)

    def test_get_updates_long_polls_with_offset_and_a_longer_http_timeout(self):
        self.wire.batches.append([message("Salom")])
        updates = self.connector.get_updates(5, timeout=25)
        self.assertEqual([u["update_id"] for u in updates], [1])
        method, body, timeout = self.wire.calls[0]
        self.assertEqual(method, "getUpdates")
        self.assertEqual(body, {"timeout": 25, "allowed_updates": ["message", "channel_post"], "offset": 5})
        self.assertGreater(timeout, 25)

    def test_empty_batch_is_valid_and_failures_hide_the_token(self):
        self.assertEqual(self.connector.get_updates(), [])
        self.assertNotIn("offset", self.wire.calls[0][1])
        self.wire.fail.add("getUpdates")
        with self.assertRaises(ConnectorError) as caught:
            self.connector.get_updates()
        self.assertNotIn(TG_TOKEN, str(caught.exception))

    def test_get_me_and_missing_credentials(self):
        self.assertEqual(self.connector.get_me()["username"], "synthetic_demo_bot")
        for token in ("", "not-a-token"):
            with self.assertRaises(ValueError):
                TelegramConnector(token, transport=self.wire).get_updates()


class StartupMessageTest(unittest.TestCase):
    def run_main(self, transport):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, \
                patch("estate.polling.LOCK_FILE", Path(folder) / "bot.lock"), \
                patch.dict(os.environ, {"ESTATE_DEMO_BOT_TOKEN": TG_TOKEN}, clear=True), \
                patch("estate.connectors._http", transport), redirect_stdout(output):
            code = main(["--env-file", "/nonexistent/estate/.env"])
        return code, output.getvalue()

    def test_a_second_copy_refuses_to_start_until_the_first_stops(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bot.lock"
            first, _ = single_copy(path)
            second, other = single_copy(path)
            self.assertIsNone(second)
            self.assertEqual(other, str(os.getpid()))
            with patch("estate.polling.LOCK_FILE", path), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--env-file", "/nonexistent/estate/.env"]), 1)
            self.assertIn("Another copy of this bot is already running", output.getvalue())
            first.close()
            again, _ = single_copy(path)
            self.assertIsNotNone(again)
            again.close()

    def test_missing_certificates_get_their_own_advice(self):
        def transport(request, *, timeout):
            raise URLError(ssl.SSLCertVerificationError("certificate verify failed"))
        code, output = self.run_main(transport)
        self.assertEqual(code, 1)
        self.assertIn("security certificate", output)
        self.assertNotIn(TG_TOKEN, output)

    def test_refused_token(self):
        code, output = self.run_main(lambda request, *, timeout: (401, b""))
        self.assertEqual(code, 1)
        self.assertIn("Telegram refused ESTATE_DEMO_BOT_TOKEN", output)


class EnvFileTest(unittest.TestCase):
    def test_reads_values_without_overriding_the_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# comment\nSYNTH_A=one\nexport SYNTH_B = 'two words'\nSYNTH_C=kept\n"
                        "not a setting\nSYNTH_EMPTY=\n")
            with patch.dict(os.environ, {"SYNTH_C": "from shell"}, clear=True):
                load_env_file(path)
                self.assertEqual({k: os.environ[k] for k in sorted(os.environ)},
                                 {"SYNTH_A": "one", "SYNTH_B": "two words",
                                  "SYNTH_C": "from shell", "SYNTH_EMPTY": ""})

    def test_missing_file_is_ignored(self):
        with patch.dict(os.environ, {}, clear=True):
            load_env_file("/nonexistent/estate/.env")
            self.assertEqual(dict(os.environ), {})


class DemoFlowTest(unittest.TestCase):
    """The four flows shown in the live demo, end to end through the real service."""

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket", side_effect=AssertionError("live network forbidden")).start()
        self.wire = FakeTelegram()
        self.connector = TelegramConnector(TG_TOKEN, "@synthetic_listings", transport=self.wire)
        self.service, self.stripped = build_service(
            self.connector, realtor_ids=(str(REALTOR),), contact=CONTACT, channel="@synthetic_listings")
        self.lines, self.offset, self.next_id = [], None, 1

    def say(self, text, sender=CLIENT):
        """Deliver one message through a poll and return the text(s) the bot sent back."""
        self.wire.batches.append([message(text, sender=sender, message_id=self.next_id)])
        self.next_id += 1
        before = len(self.wire.sent())
        self.offset = poll_once(self.service, self.connector, self.offset, log=self.lines.append)
        return [body.get("text") or body.get("caption") for _, body in self.wire.sent()[before:]]

    def test_local_photo_names_are_dropped_so_cards_can_be_sent(self):
        self.assertEqual(len(self.stripped), 6)
        replies = self.say("2 xonali Yunusobodda 600$ gacha")
        self.assertEqual([m for m, _ in self.wire.sent()], ["sendMessage", "sendMessage"])
        self.assertIn("Rasmlar: 0", replies[0])

    def test_uzbek_search_then_taken_place_is_never_offered_again(self):
        replies = self.say("2 xonali Yunusobodda 600$ gacha")
        self.assertEqual([r.split("]")[0] for r in replies], ["[L-005", "[L-003"])
        self.assertEqual(self.say("L-003 band", sender=REALTOR), ["L-003 band deb belgilandi."])
        self.assertIn("realtor 700: L-003 band deb belgilandi.", self.lines)
        replies = self.say("2 xonali Yunusobodda 600$ gacha")
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0].startswith("[L-005]"))
        self.assertNotIn("L-003", "".join(replies))

    def test_a_client_cannot_mark_places_taken(self):
        self.say("L-003 taken")
        replies = self.say("2 xonali Yunusobodda 600$ gacha", sender=CLIENT + 1)
        self.assertIn("L-003", "".join(replies))

    def test_russian_request_asks_for_missing_details_then_remembers_the_district(self):
        replies = self.say("Нужна квартира в Чиланзаре")
        self.assertEqual(replies, ["Подскажите, пожалуйста: ваш бюджет в месяц (USD), сколько комнат?"])
        replies = self.say("3 комнаты, до 700$")
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0].startswith("[L-002]"))
        self.assertIn("$600/месяц | Чиланзар | 3 комн.", replies[0])

    def test_viewing_request_is_handed_to_the_realtor(self):
        self.say("2 xonali Yunusobodda 600$ gacha")
        replies = self.say("L-005 ni ko'rsam bo'ladimi?")
        self.assertEqual(replies, [f"Ko'rish va kelishuv uchun rieltorning o'zi bilan gaplashing: {CONTACT}"])
        self.assertNotIn("+998", "".join(self.say("Хочу посмотреть L-002")))

    def test_channel_post_with_photo_becomes_a_listing_with_that_photo(self):
        post = {"update_id": 50, "channel_post": {
            "message_id": 9, "date": 1000, "caption": "Chilonzor, 2 xonali, 60 m2, 450$",
            "chat": {"id": -1001234567890, "type": "channel", "username": "synthetic_listings"},
            "photo": [{"file_id": "AgACAgSyntheticPhoto", "width": 800, "height": 600}]}}
        self.wire.batches.append([post])
        self.offset = poll_once(self.service, self.connector, self.offset, log=self.lines.append)
        self.assertEqual(self.lines[-1], "channel post saved as a new listing")
        self.say("Chilonzorda 2 xonali 500$ gacha")
        method, body = self.wire.sent()[-1]
        self.assertEqual((method, body["photo"]), ("sendPhoto", "AgACAgSyntheticPhoto"))
        self.assertIn("https://t.me/synthetic_listings/9", body["caption"])

    def test_bilingual_channel_post_keeps_its_own_id_for_realtor_commands(self):
        from tools.post_demo_listings import post_text
        row = {"id": "L-007", "title": "Chilonzorda 2 xonali / 2-комнатная в Чиланзаре",
               "district": "Chilonzor", "rooms": 2, "area_m2": 60, "rent_usd": 450,
               "amenities": ["parking"], "status": "available"}
        self.wire.batches.append([{"update_id": 51, "channel_post": {
            "message_id": 10, "date": 1000, "text": post_text(row, CONTACT),
            "chat": {"id": -1001234567890, "type": "channel", "username": "synthetic_listings"}}}])
        self.offset = poll_once(self.service, self.connector, self.offset, log=self.lines.append)
        self.assertEqual(self.lines[-1], "channel post saved as a new listing")
        listing = self.service.registry.get("demo").kb.get("L-007")
        self.assertEqual((listing.district, listing.rooms, listing.rent_usd), ("Chilonzor", 2, 450))
        self.assertTrue(self.say("Chilonzorda 2 xonali 500$ gacha")[0].startswith("[L-007] 🏠 Chilonzorda"))
        self.assertEqual(self.say("L-007 занято", sender=REALTOR), ["L-007 отмечено как занятое."])
        self.assertNotIn("L-007", "".join(self.say("Chilonzorda 2 xonali 500$ gacha")))

    def test_both_languages_only_until_the_client_shows_one(self):
        welcome = self.say("/start")[0]
        self.assertIn("Assalomu alaykum", welcome)
        self.assertIn("Здравствуйте", welcome)
        self.assertEqual(self.say("Hello, I need a flat"),
                         ["Iltimos, ayting: oylik byudjetingiz (USD), qaysi tuman, nechta xona?\n\n"
                          "Подскажите, пожалуйста: ваш бюджет в месяц (USD), какой район, сколько комнат?"])
        self.assertEqual(self.say("Нужно в Юнусабаде"),
                         ["Подскажите, пожалуйста: ваш бюджет в месяц (USD), сколько комнат?"])
        # "2 room, 600$" shows no Uzbek or Russian, so the reply and cards stay Russian.
        replies = self.say("2 room, 600$")
        self.assertEqual([r.split("\n")[0] for r in replies],
                         ["[L-005] 2-комнатная у метро Минор", "[L-003] Юнусабад-4, 2-комнатная в новостройке"])
        self.assertIn("/месяц | Юнусабад", replies[0])

    def test_realtor_gets_a_menu_and_a_listing_overview_not_client_replies(self):
        menu = self.say("/start", sender=REALTOR)[0]
        self.assertIn("Siz rieltor sifatida yozyapsiz", menu)
        self.assertIn("Вы пишете как риэлтор", menu)
        self.assertEqual(self.say("Привет", sender=REALTOR)[0].splitlines()[0], "Вы пишете как риэлтор. Команды:")
        self.say("L-003 band", sender=REALTOR)
        overview = self.say("/list", sender=REALTOR)[0]
        for row in ("✅ L-001 · bo'sh · Chorsu bozori yonida 1 xonali kvartira · $350",
                    "⛔ L-003 · band · Yunusobod-4, yangi uyda 2 xonali · $550",
                    "⛔ L-004 · занято · Уютная 2-комнатная на Мирзо-Улугбеке · $450"):
            self.assertIn(row, overview)
        self.assertIn("realtor 700: E'lonlaringiz:", self.lines)

    def test_log_shows_the_senders_username(self):
        update = message("Salom", message_id=90)
        update["message"]["from"]["username"] = "client_one"
        self.wire.batches.append([update])
        poll_once(self.service, self.connector, None, log=self.lines.append)
        self.assertTrue(self.lines[-1].startswith("client 111 @client_one: "))

    def test_a_failed_send_is_logged_and_polling_continues(self):
        self.wire.fail.add("sendMessage")
        self.say("2 xonali Yunusobodda 600$ gacha")
        self.assertIn("not answered", self.lines[-1])
        self.assertNotIn(TG_TOKEN, "".join(self.lines))
        self.assertEqual(self.offset, 2)
        self.wire.fail.clear()
        self.assertEqual(len(self.say("2 xonali Yunusobodda 600$ gacha")), 2)


if __name__ == "__main__":
    unittest.main()
