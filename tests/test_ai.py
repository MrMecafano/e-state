"""Offline tests for model-written replies. A fake client stands in for the Claude API."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from estate.ai import AIUnavailable, ClaudeResponder, Draft, listing_facts
from estate.agent import respond
from estate.connectors import TelegramConnector
from estate.extract import extract
from estate.kb import DEMO_DATA, KnowledgeBase
from estate.matcher import Criteria
from estate.polling import build_service, poll_once
from tests.test_polling import CONTACT, REALTOR, TG_TOKEN, FakeTelegram, message


def answer(reply="", listing_ids=(), handover=False, stop_reason="end_turn", raw=None,
           district="", rooms=0, budget_usd=0):
    text = raw if raw is not None else json.dumps(
        {"reply": reply, "listing_ids": list(listing_ids), "handover": handover,
         "district": district, "rooms": rooms, "budget_usd": budget_usd})
    return SimpleNamespace(stop_reason=stop_reason, content=[
        SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)])


class FakeClaude:
    """Records each request; returns queued answers or raises queued errors."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=lambda **kw: self._create(beta=False, **kw))

    def _create(self, beta=True, **request):
        self.requests.append(dict(request, beta=beta))
        result = self.answers.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ResponderRequestTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase.from_json("demo")
        self.claude = FakeClaude(answer("Mana ikkita variant.", ["L-005", "L-003"]))
        self.draft = ClaudeResponder(self.claude).draft(
            self.kb.available(),
            [{"role": "assistant", "text": "stray"}, {"role": "user", "text": "Salom"},
             {"role": "user", "text": "Yunusobod"}, {"role": "assistant", "text": "Byudjet?"}],
            "600$ gacha")
        self.request = self.claude.requests[0]

    def test_sends_every_free_listing_without_owner_details(self):
        stock = self.request["system"][-1]["text"]
        listings = json.loads(stock.split("\n", 1)[1])
        self.assertEqual([l["id"] for l in listings], ["L-001", "L-002", "L-003", "L-005", "L-006"])
        self.assertNotIn("L-004", stock)  # taken
        with open(DEMO_DATA, encoding="utf-8") as f:
            rows = json.load(f)
        for row in rows:
            self.assertNotIn(row["owner_name"], stock)
            self.assertNotIn(row["owner_phone"], stock)
        self.assertNotIn("owner", json.dumps(listing_facts(self.kb.get("L-001"))))

    def test_request_shape(self):
        self.assertEqual(self.request["model"], "claude-opus-5-5")
        self.assertTrue(self.request["beta"])
        self.assertEqual((self.request["betas"], self.request["fallbacks"]),
                         (["server-side-fallback-2026-07-01"], "default"))
        self.assertEqual(self.request["output_config"]["effort"], "low")
        self.assertEqual(self.request["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(self.request["system"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn(CONTACT, json.dumps(self.request["system"], ensure_ascii=False))
        self.assertEqual(self.request["messages"], [
            {"role": "user", "content": "Salom\n\nYunusobod"},
            {"role": "assistant", "content": "Byudjet?"},
            {"role": "user", "content": [
                {"type": "text", "text": "600$ gacha"},
                {"type": "text", "text": "(Reply language: Uzbek, Latin script.)"}]}])
        self.assertEqual(self.draft, Draft("Mana ikkita variant.", ("L-005", "L-003"), False))

    def test_other_models_skip_the_fallback_beta(self):
        claude = FakeClaude(answer("Salom"))
        ClaudeResponder(claude, "claude-haiku-5-5").draft([], [], "Salom")
        self.assertFalse(claude.requests[0]["beta"])
        self.assertNotIn("fallbacks", claude.requests[0])

    def test_bad_answers_become_ai_unavailable(self):
        for result in (answer(stop_reason="refusal"), answer(stop_reason="max_tokens"),
                       answer(raw="not json"), answer(raw='{"reply": 5}'),
                       ConnectionError("synthetic network failure sk-ant-secret")):
            with self.subTest(result=result), self.assertRaises(AIUnavailable) as caught:
                ClaudeResponder(FakeClaude(result)).draft([], [], "Salom")
            self.assertNotIn("sk-ant-secret", str(caught.exception))

    def test_off_without_a_key(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ClaudeResponder.from_environment())


class StubResponder:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def draft(self, listings, history, message, lang):
        self.lang = lang
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class RespondTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase.from_json("demo")
        self.kb.apply_control_message("L-003 taken")

    def turn(self, result, text="Yunusobod tomonda arzonroq narsa bormi?"):
        self.responder = StubResponder(result)
        return respond(self.kb, text, Criteria(), CONTACT, responder=self.responder)

    def test_model_text_comes_first_and_cards_come_from_the_knowledge_base(self):
        turn = self.turn(Draft("Bitta [L-005] variant bor.", ("L-005", "L-003", "L-999"), False,
                               "Yunusobod", 2, 600))
        self.assertTrue(turn.ai)
        intro, card = turn.reply.split("\n\n", 1)
        self.assertEqual(intro, "Bitta L-005 variant bor.")
        self.assertTrue(card.startswith("[L-005] Minor metrosi yonida 2 xonali"))
        self.assertNotIn("[L-003]", turn.reply)
        self.assertNotIn("L-999", turn.reply)

    def test_taken_place_in_model_text_falls_back_to_rules(self):
        turn = self.turn(Draft("L-003 hali ham bo'sh!", (), False))
        self.assertFalse(turn.ai)
        self.assertEqual(turn.note, "AI draft rejected by the checker")
        self.assertNotIn("L-003", turn.reply)

    def test_owner_phone_or_money_request_falls_back_to_rules(self):
        for text in ("Egasiga qo'ng'iroq qiling: +998977778899", "Please send a $100 deposit first."):
            with self.subTest(text=text):
                turn = self.turn(Draft(text, (), False))
                self.assertFalse(turn.ai)
                self.assertNotIn("+998", turn.reply)

    def test_model_handover_always_carries_the_realtor_contact(self):
        turn = self.turn(Draft("Albatta, rieltor yordam beradi.", ("L-005",), True),
                         text="Qachon borsam bo'ladi?")
        self.assertTrue(turn.ai and turn.handover)
        self.assertIn(CONTACT, turn.reply)
        self.assertNotIn("[L-005]", turn.reply)

    def test_start_and_payment_skip_the_model_but_viewing_goes_to_it(self):
        for text in ("/start", "Oldindan to'lov kerakmi?"):
            with self.subTest(text=text):
                turn = self.turn(Draft("x", (), False), text=text)
                self.assertEqual(self.responder.calls, 0)
                self.assertFalse(turn.ai)
        turn = self.turn(Draft("Albatta.", (), True), text="L-005 ni ko'rsam bo'ladimi?")
        self.assertEqual(self.responder.calls, 1)
        self.assertIn(CONTACT, turn.reply)

    def test_no_list_until_district_rooms_and_budget_are_known(self):
        early, text = Draft("Mana variantlar.", ("L-005",), False, "Yunusobod", 2, 0), "Yunusobodda 2 xonali kerak"
        turn = respond(self.kb, text, extract(text), CONTACT, responder=StubResponder(early))
        self.assertEqual((turn.ai, turn.note), (False, "AI listed places before knowing district, rooms and budget"))
        self.assertEqual(turn.reply, "Iltimos, ayting: oylik byudjetingiz (USD)?")
        # A budget the code read earlier in the chat counts as known.
        turn = respond(self.kb, text, Criteria(budget_usd=600), CONTACT, responder=StubResponder(early))
        self.assertTrue(turn.ai)
        self.assertIn("[L-005]", turn.reply)

    def test_language_is_both_only_before_the_client_shows_one(self):
        self.turn(Draft("x", (), False), text="2 room, 600$")
        self.assertEqual(self.responder.lang, "both")
        responder = StubResponder(Draft("x", (), False))
        turn = respond(self.kb, "2 room, 600$", Criteria(), CONTACT, responder=responder,
                       history=[{"role": "user", "text": "Нужна квартира"}])
        self.assertEqual((responder.lang, turn.lang), ("ru", "ru"))
        claude = FakeClaude(answer("x"))
        ClaudeResponder(claude).draft([], [], "Hello", "both")
        self.assertIn("then the same in Russian", claude.requests[0]["messages"][-1]["content"][1]["text"])

    def test_contact_only_in_a_handover(self):
        turn = self.turn(Draft(f"Rieltorga yozing: {CONTACT}", (), False))
        self.assertEqual((turn.ai, turn.note), (False, "AI gave the realtor contact unasked"))
        self.assertNotIn(CONTACT, turn.reply)

    def test_unavailable_model_falls_back_to_rules(self):
        turn = self.turn(AIUnavailable("rate limited"))
        self.assertEqual((turn.ai, turn.note), (False, "AI unavailable: rate limited"))
        self.assertTrue(turn.reply)


class TelegramAIFlowTest(unittest.TestCase):
    """Model replies through the real service and Telegram connector."""

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket", side_effect=AssertionError("live network forbidden")).start()
        self.wire = FakeTelegram()
        self.connector = TelegramConnector(TG_TOKEN, transport=self.wire)
        self.claude = FakeClaude()
        self.service, _ = build_service(self.connector, realtor_ids=(str(REALTOR),), contact=CONTACT,
                                        responder=ClaudeResponder(self.claude))
        self.lines, self.offset, self.next_id = [], None, 1

    def say(self, text, sender=111):
        self.wire.batches.append([message(text, sender=sender, message_id=self.next_id)])
        self.next_id += 1
        before = len(self.wire.sent())
        self.offset = poll_once(self.service, self.connector, self.offset, log=self.lines.append)
        return [body.get("text") for _, body in self.wire.sent()[before:]]

    def test_ai_message_then_cards_and_history_on_the_next_turn(self):
        self.claude.answers.append(answer("Yunusobodda ikkita mos variant bor.", ["L-005", "L-003"],
                                          district="Yunusobod", rooms=2, budget_usd=600))
        replies = self.say("Yunusobod tomonda arzonroq narsa bormi?")
        self.assertEqual(replies[0], "Yunusobodda ikkita mos variant bor.")
        self.assertEqual([r.split("]")[0] for r in replies[1:]], ["[L-005", "[L-003"])
        self.assertEqual(self.lines[-1], "client 111: AI replied with L-005, L-003")
        self.say("L-003 taken", sender=REALTOR)
        self.claude.answers.append(answer("Hozircha bittasi qoldi.", ["L-005"],
                                          district="Yunusobod", rooms=2, budget_usd=600))
        self.say("Yana bormi?")
        second = self.claude.requests[-1]
        self.assertNotIn('"L-003"', second["system"][-1]["text"])
        self.assertEqual([m["role"] for m in second["messages"]], ["user", "assistant", "user"])
        self.assertIn("[L-003]", second["messages"][1]["content"])

    def test_failure_is_logged_and_rules_still_answer(self):
        self.claude.answers.append(ConnectionError("synthetic"))
        replies = self.say("2 xonali Yunusobodda 600$ gacha")
        self.assertEqual(len(replies), 2)
        self.assertEqual(self.lines[-1],
                         "client 111: rules replied with L-005, L-003 (AI unavailable: ConnectionError)")

    def test_model_handover_counts_as_a_handover(self):
        self.claude.answers.append(answer("Rieltor sizga yordam beradi.", [], True))
        self.assertIn(CONTACT, self.say("Qachon uchrashamiz?")[0])
        self.assertEqual(self.service.stats.stats("demo").handovers, 1)


if __name__ == "__main__":
    unittest.main()
