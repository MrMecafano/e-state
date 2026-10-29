"""Offline tests for the realtor assistant. A fake client stands in for the Claude API."""
import json
import unittest
from unittest.mock import patch

from estate.ai import ClaudeResponder
from estate.connectors import TelegramConnector
from estate.polling import build_service, poll_once
from tests.test_ai import FakeClaude, answer
from tests.test_polling import CONTACT, REALTOR, TG_TOKEN, FakeTelegram, message


def realtor_answer(reply="", changes=()):
    return answer(raw=json.dumps({"reply": reply, "changes": [
        {"listing_id": listing_id, "status": status} for listing_id, status in changes]}))


class RealtorAssistantTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket", side_effect=AssertionError("live network forbidden")).start()
        self.wire, self.claude = FakeTelegram(), FakeClaude()
        self.connector = TelegramConnector(TG_TOKEN, transport=self.wire)
        self.service, _ = build_service(self.connector, realtor_ids=(str(REALTOR),), contact=CONTACT,
                                        responder=ClaudeResponder(self.claude))
        self.kb = self.service.registry.get("demo").kb
        self.lines, self.next_id = [], 1

    def say(self, text, sender=REALTOR):
        self.wire.batches.append([message(text, sender=sender, message_id=self.next_id)])
        self.next_id += 1
        before = len(self.wire.sent())
        poll_once(self.service, self.connector, None, log=self.lines.append)
        return "\n\n".join(body.get("text", "") for _, body in self.wire.sent()[before:])

    def test_plain_sentence_proposes_a_change_and_only_yes_applies_it(self):
        self.claude.answers.append(realtor_answer(changes=[("L-005", "taken")]))
        self.assertEqual(self.say("Minor metrodagi uy ijaraga berildi"),
                         "Tasdiqlaysizmi?\nL-005 (Minor metrosi yonida 2 xonali) → band\nHa / Yo'q")
        self.assertTrue(self.kb.get("L-005").available)
        self.assertEqual(self.lines[-1], "realtor 700: AI: Tasdiqlaysizmi?")
        self.assertEqual(self.say("Ha"), "L-005 band deb belgilandi.")
        self.assertFalse(self.kb.get("L-005").available)
        self.assertEqual(len(self.claude.requests), 1)  # "Ha" is handled by code, not the model

    def test_no_cancels_and_a_client_cannot_confirm(self):
        self.claude.answers.append(realtor_answer(changes=[("L-005", "taken")]))
        self.say("Minor metrodagi uy ijaraga berildi")
        self.say("Ha", sender=111)  # a client's "Ha" is a client message
        self.assertTrue(self.kb.get("L-005").available)
        self.assertEqual(self.say("Yo'q"), "Bekor qilindi, hech narsa o'zgarmadi.")
        self.assertTrue(self.kb.get("L-005").available)

    def test_russian_confirmation_and_reply(self):
        self.claude.answers.append(realtor_answer("Понял.", changes=[("L-005", "taken")]))
        self.assertEqual(self.say("Квартиру у метро Минор сдали"),
                         "Понял.\n\nПодтверждаете?\nL-005 (2-комнатная у метро Минор) → занято\nДа / Нет")
        self.assertEqual(self.say("Да"), "L-005 отмечено как занятое.")

    def test_unknown_and_unchanged_proposals_are_dropped(self):
        self.claude.answers.append(realtor_answer("L-004 allaqachon band.", [("L-999", "taken"), ("L-004", "taken")]))
        self.assertEqual(self.say("L-004 ni band qil"), "L-004 allaqachon band.")
        self.say("Ha")
        self.assertIsNone(self.kb.get("L-999"))

    def test_confirmation_expires_after_ten_minutes(self):
        now = [1000.0]
        self.service.clock = lambda: now[0]
        self.claude.answers.append(realtor_answer(changes=[("L-005", "taken")]))
        self.say("Minor metrodagi uy ijaraga berildi")
        now[0] += 601
        self.claude.answers.append(realtor_answer("Nima tasdiqlansin?"))
        self.assertEqual(self.say("Ha"), "Nima tasdiqlansin?")
        self.assertTrue(self.kb.get("L-005").available)

    def test_questions_get_listings_status_and_counts_but_no_private_data(self):
        self.say("2 xonali Yunusobodda 600$ gacha", sender=111)
        self.claude.answers.extend([realtor_answer("Bitta mijoz yozdi."), realtor_answer("Ikkita.")])
        self.assertEqual(self.say("Nechta mijoz yozdi?"), "Bitta mijoz yozdi.")
        self.say("Yunusobodda nechta bo'sh uy bor?")
        request = self.claude.requests[-1]
        system = json.dumps(request["system"], ensure_ascii=False)
        self.assertIn('\\"status\\": \\"taken\\"', system)  # L-004
        stats = json.loads(request["system"][-1]["text"].split("\n", 1)[1])
        self.assertEqual(stats["clients_total"], 1)
        for private in ("Dilshod", "Rustam", "+998", "111", CONTACT):
            self.assertNotIn(private, system)
        self.assertEqual([m["role"] for m in request["messages"]], ["user", "assistant", "user"])

    def test_commands_list_and_start_skip_the_model(self):
        self.assertEqual(self.say("L-003 band"), "L-003 band deb belgilandi.")
        self.assertIn("⛔ L-003 · band", self.say("/list"))
        menu = self.say("/start")
        self.assertIn("Oddiy gap bilan ham yozing", menu)
        self.assertIn("Можно писать и обычными словами", menu)
        self.assertEqual(self.claude.requests, [])

    def test_unavailable_model_shows_the_menu(self):
        self.claude.answers.append(ConnectionError("synthetic"))
        self.assertIn("Siz rieltor sifatida yozyapsiz", self.say("Salom, nima gap?"))


if __name__ == "__main__":
    unittest.main()
