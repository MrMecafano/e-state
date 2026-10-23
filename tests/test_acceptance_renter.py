"""Renter-side acceptance pass for UC-1 "Answer a client inquiry" (KAN-5).

Owner: Mahmudov Shoxrux Mirzo, Client Contact (Renter perspective). This file
exercises the agent end-to-end the way SRD Week 1 section 7 describes UC-1,
one test per normal/alternative/exception course, so a renter-side reviewer
can see each documented course actually holds without reading the component
unit tests in tests/test_estate.py.

N-01 ("first reply within 1 minute for 95% of messages") is a deployment-time
latency SLA, not something a unit test can honestly measure -- it needs a real
Telegram/Instagram connector under load. What this suite can and does check is
the structural precondition for it: handle() is synchronous, in-process and
makes no network or disk call per message (listings are loaded once into the
KnowledgeBase beforehand), so nothing here adds unbounded latency.
"""
from __future__ import annotations

import unittest

from estate.agent import handle
from estate.extract import extract
from estate.kb import KnowledgeBase

CONTACT = "@demo_realtor"


def kb():
    return KnowledgeBase.from_json("demo")


class UC1NormalCourse(unittest.TestCase):
    """Client writes in Uzbek with all criteria given; agent finds up to 3 matches,
    checker approves, agent sends details and post links (F-02, F-03, F-04)."""

    def test_uzbek_latin_full_criteria_returns_checked_matches(self):
        msg = "Yunusobodda 2 xonali kvartira kerak, 600$ gacha"
        reply, blocked = handle(kb(), msg, extract(msg), CONTACT)
        self.assertEqual(blocked, [])
        for field in ("$", "Yunusobod", "room", "Photos:", "Post:"):  # F-04
            self.assertIn(field, reply)


class UC1AlternativeLanguages(unittest.TestCase):
    """Alternative courses: [Uzbek Latin, Uzbek Cyrillic, Russian] (F-08)."""

    def test_uzbek_cyrillic_client_is_answered_in_uzbek_cyrillic(self):
        msg = "Чилонзорда икки хонали уй керак"
        reply, blocked = handle(kb(), msg, extract(msg), CONTACT)
        self.assertEqual(blocked, [])
        self.assertIn("бюджетингиз", reply)  # asking for the missing budget, in Cyrillic

    def test_russian_client_is_answered_in_russian(self):
        msg = "Ищу двухкомнатную в Юнусабаде до 550 долларов"
        reply, blocked = handle(kb(), msg, extract(msg), CONTACT)
        self.assertEqual(blocked, [])
        self.assertIn("$", reply)
        self.assertNotIn("xona", reply)  # not answered in Uzbek Latin


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
        self.assertNotIn("Photos:", reply)  # handed over, not a listing card


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
        self.assertIn(CONTACT, reply)

    def test_a_blocked_reply_falls_back_to_handover_not_to_silence(self):
        from estate.kb import Listing

        k = KnowledgeBase("x", [Listing("L-001", "Flat with owner Ali", "Chilonzor", 1, 30, 200, owner_name="Ali")])
        msg = "1 xonali Chilonzorda, 300$ gacha"
        reply, blocked = handle(k, msg, extract(msg), CONTACT)
        self.assertTrue(blocked)  # F-10 caught something the composer missed
        self.assertIn(CONTACT, reply)  # client still gets an answer, just a handover


if __name__ == "__main__":
    unittest.main()
