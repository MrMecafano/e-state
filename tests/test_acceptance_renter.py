"""Renter-side acceptance pass for UC-1 "Answer a client inquiry" (KAN-5).

Owner: Mahmudov Shoxrux Mirzo, Client Contact (Renter perspective). This file
exercises the agent end-to-end the way SRD Week 1 section 7 describes UC-1,
one test per normal/alternative/exception course, so a renter-side reviewer
can see each documented course actually holds without reading the component
unit tests in tests/test_estate.py.

N-01 ("first reply within 1 minute for 95% of messages") is a deployment-time
latency SLA, not something a unit test can honestly measure -- it needs a real
Telegram/Instagram connector under load. These acceptance tests do not measure
that SLA or enforce the absence of per-message network/disk I/O. Latency and
I/O guarantees require separate instrumentation and deployment benchmarks.
"""
from __future__ import annotations

import re
import unittest

from estate.agent import handle
from estate.extract import extract
from estate.kb import KnowledgeBase, Listing
from estate.replies import _DISTRICT_NAMES

CONTACT = "@demo_realtor"


def kb():
    return KnowledgeBase.from_json("demo")


class RenterAcceptanceCase(unittest.TestCase):
    def assert_matched_cards(self, k, criteria, reply, blocked, expected_ids):
        self.assertEqual(blocked, [])
        self.assertEqual(criteria.missing(), [])
        ids = re.findall(r"^\[(L-\d+)\]", reply, re.M)
        self.assertCountEqual(ids, expected_ids)
        self.assertLessEqual(len(ids), 3)
        cards = reply.split("\n\n")
        self.assertEqual(len(cards), len(ids))
        for listing_id, card in zip(ids, cards):
            listing = k.get(listing_id)
            self.assertIsNotNone(listing)
            self.assertTrue(listing.available)
            self.assertLessEqual(listing.rent_usd, criteria.budget_usd)
            self.assertEqual(listing.district, criteria.district)
            self.assertEqual(listing.rooms, criteria.rooms)
            # Check displayed facts, not just that a valid ID was mentioned.
            details = card.splitlines()[1]
            self.assertIn(f"${listing.rent_usd}/", details)
            # Cyrillic cards show the district in their own script.
            shown = details.split(" | ")[1]
            self.assertIn(shown, (listing.district, *_DISTRICT_NAMES.get(listing.district, ())))
            self.assertIn(f"| {listing.rooms} ", details)
            self.assertEqual(re.findall(r"https?://\S+", card), [listing.post_url])

    def assert_card_language(self, reply, lang):
        month, rooms, amenities, photos, post = {
            "uz-latn": ("oy", "xona", "Qulayliklar:", "Rasmlar:", "E'lon:"),
            "uz-cyrl": ("ой", "хона", "Қулайликлар:", "Расмлар:", "Эълон:"),
            "ru": ("месяц", "комн.", "Удобства:", "Фото:", "Объявление:"),
        }[lang]
        for card in reply.split("\n\n"):
            lines = card.splitlines()
            self.assertGreaterEqual(len(lines), 4)
            # Source titles need not be translated. Only
            # the presentation controlled by the application is checked here.
            self.assertIn(f"/{month} |", lines[1])
            self.assertRegex(lines[1], rf"\| \d+ {re.escape(rooms)},")
            self.assertTrue(lines[2].startswith(amenities), lines[2])
            self.assertTrue(lines[3].startswith(photos), lines[3])
            self.assertIn(f" | {post} ", lines[3])
            presentation = "\n".join(lines[1:])
            for english in ("/month", "room(s)", "Amenities:", "Photos:", "Post:"):
                self.assertNotIn(english, presentation)


class UC1NormalCourse(RenterAcceptanceCase):
    """Client writes in Uzbek with all criteria given; agent finds up to 3 matches,
    checker approves, agent sends details and post links (F-02, F-03, F-04)."""

    def test_uzbek_latin_full_criteria_returns_checked_matches(self):
        msg = "Yunusobodda 2 xonali kvartira kerak, 600$ gacha"
        k, criteria = kb(), extract(msg)
        reply, blocked = handle(k, msg, criteria, CONTACT)
        self.assert_matched_cards(k, criteria, reply, blocked, ["L-005", "L-003"])
        self.assert_card_language(reply, "uz-latn")
        self.assertEqual(
            re.findall(r"https?://\S+", reply),
            ["https://t.me/c/4496013481/7", "https://t.me/c/4496013481/5"],
        )

    def test_more_than_three_matches_returns_only_three(self):
        k = KnowledgeBase("limit", [
            Listing(f"L-{200 + n}", f"Source title {n}", "Yunusobod", 2, 50,
                    250 + 50 * n, post_url=f"https://t.me/estate_demo/{200 + n}")
            for n in range(1, 5)
        ])
        msg = "Yunusobodda 2 xonali kvartira kerak, 600$ gacha"
        criteria = extract(msg)
        reply, blocked = handle(k, msg, criteria, CONTACT)
        self.assert_matched_cards(k, criteria, reply, blocked, ["L-201", "L-202", "L-203"])
        self.assert_card_language(reply, "uz-latn")


class UC1AlternativeLanguages(RenterAcceptanceCase):
    """Alternative courses: [Uzbek Latin, Uzbek Cyrillic, Russian] (F-08)."""

    def test_uzbek_cyrillic_client_is_answered_in_uzbek_cyrillic(self):
        msg = "Чилонзорда икки хонали уй керак"
        reply, blocked = handle(kb(), msg, extract(msg), CONTACT)
        self.assertEqual(blocked, [])
        self.assertIn("бюджетингиз", reply)  # asking for the missing budget, in Cyrillic

    def test_uzbek_cyrillic_complete_criteria_returns_cyrillic_cards(self):
        msg = "Юнусободда икки хонали уй керак, 600$ гача"
        k, criteria = kb(), extract(msg)
        reply, blocked = handle(k, msg, criteria, CONTACT)
        self.assert_matched_cards(k, criteria, reply, blocked, ["L-005", "L-003"])
        self.assert_card_language(reply, "uz-cyrl")

    def test_russian_client_is_answered_in_russian(self):
        msg = "Ищу двухкомнатную в Юнусабаде до 550 долларов"
        k, criteria = kb(), extract(msg)
        reply, blocked = handle(k, msg, criteria, CONTACT)
        self.assert_matched_cards(k, criteria, reply, blocked, ["L-005", "L-003"])
        self.assert_card_language(reply, "ru")


class UC1AlternativeCriteria(unittest.TestCase):
    """Alternative courses: Criteria [complete, partial: agent asks] (F-03)."""

    def test_partial_criteria_asks_only_for_what_is_missing(self):
        msg = "Yunusobodda kvartira kerak"  # district given, budget and rooms missing
        c = extract(msg)
        reply, blocked = handle(kb(), msg, c, CONTACT)
        self.assertEqual(blocked, [])
        self.assertIn("byudjet", reply)
        self.assertIn("xona", reply)
        self.assertNotIn("tuman", reply)  # district was given, so not asked again


class UC1AlternativeViewing(unittest.TestCase):
    """Alternative course: client asks for a viewing -> hand over to realtor (F-07)."""

    def test_viewing_request_hands_over_instead_of_recommending(self):
        msg = "Yunusobodda 2 xonali kvartira kerak, 600$ gacha, ko'rsam bo'ladimi?"
        reply, blocked = handle(kb(), msg, extract(msg), CONTACT)
        self.assertEqual(blocked, [])
        self.assertIn(CONTACT, reply)
        self.assertNotRegex(reply, r"\[L-\d+\]")  # handed over, not a listing card

    def test_russian_viewing_and_realtor_requests_hand_over(self):
        for msg in (
            "Хочу посмотреть двухкомнатную квартиру в Юнусабаде до 550 долларов",
            "Хочу поговорить с риелтором",
            "Как связаться с риелтором?",
        ):
            with self.subTest(message=msg):
                reply, blocked = handle(kb(), msg, extract(msg), CONTACT)
                self.assertEqual(blocked, [])
                self.assertIn(CONTACT, reply)
                self.assertIn("свяжитесь с риэлтором", reply)
                self.assertNotRegex(reply, r"\[L-\d+\]")

    def test_wanting_to_rent_or_see_options_is_a_search_not_a_handover(self):
        for msg in ("Хочу снять двухкомнатную квартиру в Юнусабаде до 550 долларов",
                    "Хочу посмотреть варианты: двухкомнатная в Юнусабаде до 550 долларов",
                    "Yunusobodda 2 xonali 550$ gacha variantlarni ko'rsating"):
            with self.subTest(message=msg):
                reply, blocked = handle(kb(), msg, extract(msg), CONTACT)
                self.assertEqual(blocked, [])
                self.assertEqual(re.findall(r"\[(L-\d+)\]", reply), ["L-005", "L-003"])
                self.assertNotIn(CONTACT, reply)


class UC1Exceptions(unittest.TestCase):
    """Exceptions: no available match; checker rejects; client raises payment."""

    def test_no_match_tells_the_client_instead_of_going_silent(self):
        msg = "Bektemirda 5 xonali kvartira kerak, 50$ gacha"  # no listing fits
        reply, blocked = handle(kb(), msg, extract(msg), CONTACT)
        self.assertEqual(blocked, [])
        self.assertIn("yo'q", reply)

    def test_client_raising_payment_is_refused_not_answered_with_money_talk(self):
        msg = "Oldindan to'lov qilishim kerakmi?"
        reply, blocked = handle(kb(), msg, extract(msg), CONTACT)
        self.assertEqual(blocked, [])
        self.assertIn("oldindan to'lov so'ramaymiz", reply)  # F-11
        self.assertNotIn(CONTACT, reply)  # the contact is only for viewings and realtor requests

    def test_russian_payment_questions_are_refused(self):
        for msg in ("Как перевести оплату?", "Нужен депозит?", "Нужно внести залог?"):
            with self.subTest(message=msg):
                reply, blocked = handle(kb(), msg, extract(msg), CONTACT)
                self.assertEqual(blocked, [])
                self.assertIn("Мы не берём никакой предоплаты.", reply)
                self.assertNotIn(CONTACT, reply)
                self.assertNotRegex(reply, r"\[L-\d+\]")

    def test_a_blocked_reply_falls_back_to_handover_not_to_silence(self):
        k = KnowledgeBase("x", [Listing("L-001", "Flat with owner Ali", "Chilonzor", 1, 30, 200, owner_name="Ali")])
        msg = "1 xonali Chilonzorda, 300$ gacha"
        reply, blocked = handle(k, msg, extract(msg), CONTACT)
        self.assertTrue(blocked)  # F-10 caught something the composer missed
        self.assertIn(CONTACT, reply)  # client still gets an answer, just a handover
        self.assertNotIn("Flat with owner Ali", reply)
        self.assertNotRegex(reply, r"\bAli\b")
        self.assertNotIn("[L-001]", reply)


if __name__ == "__main__":
    unittest.main()
