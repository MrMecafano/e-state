import unittest

from estate.agent import handle
from estate.checker import check
from estate.kb import KnowledgeBase, Listing
from estate.language import detect
from estate.matcher import Criteria, recommend
from estate.replies import compose, format_listing

CONTACT = "@demo_realtor"


def kb():
    return KnowledgeBase.from_json("demo")


class KnowledgeBaseTest(unittest.TestCase):
    def test_f05_control_message_marks_taken_and_back(self):
        k = kb()
        self.assertEqual(k.apply_control_message("L-003 taken"), "L-003 marked taken.")
        self.assertFalse(k.get("L-003").available)
        self.assertEqual(k.apply_control_message("bo'sh L-003"), "L-003 marked available.")
        self.assertTrue(k.get("L-003").available)

    def test_f05_russian_and_unknown(self):
        k = kb()
        self.assertEqual(k.apply_control_message("L-002 сдана"), "L-002 marked taken.")
        self.assertIn("No listing", k.apply_control_message("L-999 taken"))
        self.assertIn("Send:", k.apply_control_message("hello"))

    def test_n02_instances_are_isolated(self):
        a, b = kb(), KnowledgeBase("other")
        a.apply_control_message("L-001 taken")
        self.assertEqual(b.all(), [])
        b.add(Listing("L-900", "x", "Chilonzor", 1, 30, 200))
        self.assertIsNone(a.get("L-900"))


class MatcherTest(unittest.TestCase):
    def test_f03_at_most_three_and_within_budget(self):
        hits = recommend(kb(), Criteria(budget_usd=1000))
        self.assertLessEqual(len(hits), 3)
        self.assertTrue(all(l.rent_usd <= 1000 for l in hits))

    def test_f06_taken_never_offered(self):
        hits = recommend(kb(), Criteria(budget_usd=500, district="Mirzo Ulug'bek", rooms=2))
        self.assertEqual(hits, [])  # L-004 matches but is taken

    def test_f03_missing_criteria(self):
        self.assertEqual(Criteria(district="Yunusobod").missing(), ["budget_usd", "rooms"])


class LanguageTest(unittest.TestCase):
    def test_f08_detection(self):
        self.assertEqual(detect("2 xonali kvartira kerak"), "uz-latn")
        self.assertEqual(detect("Менга 2 хонали уй керак"), "uz-cyrl")
        self.assertEqual(detect("Нужна двухкомнатная квартира"), "ru")


class RepliesTest(unittest.TestCase):
    def test_f03_asks_for_missing_in_client_language(self):
        r = compose(kb(), "kvartira kerak", Criteria(district="Yunusobod"), "uz-latn", CONTACT)
        self.assertIn("byudjet", r)
        self.assertIn("xona", r)

    def test_f04_f12_card_has_details_but_no_owner(self):
        card = format_listing(kb().get("L-003"))
        for part in ("$550/month", "Yunusobod", "2 room", "elevator", "https://t.me/estate_demo/103"):
            self.assertIn(part, card)
        self.assertNotIn("Rustam", card)
        self.assertNotIn("+998977778899", card)

    def test_f07_viewing_hands_over(self):
        r = compose(kb(), "Uyni ko'rsam bo'ladimi?", Criteria(500, "Yunusobod", 2), "uz-latn", CONTACT)
        self.assertIn(CONTACT, r)

    def test_f11_never_asks_for_money(self):
        r = compose(kb(), "Oldindan to'lov qilishim kerakmi?", Criteria(), "uz-latn", CONTACT)
        self.assertIn("oldindan to'lov so'ramaymiz", r)
        self.assertTrue(check(r, kb(), CONTACT).ok)


class CheckerTest(unittest.TestCase):
    def test_f10_blocks_unknown_and_taken_listing(self):
        self.assertIn("L-777 is not in the knowledge base", check("Try [L-777]", kb()).reasons)
        self.assertIn("L-004 is taken", check("Try [L-004]", kb()).reasons)

    def test_f12_blocks_owner_leak(self):
        v = check("Call the owner Rustam on +998 97 777 88 99", kb())
        self.assertFalse(v.ok)

    def test_f11_blocks_money_request(self):
        self.assertFalse(check("Please send a deposit of $100 to hold it", kb()).ok)

    def test_good_reply_passes(self):
        k = kb()
        reply = compose(k, "2 xonali Yunusobodda", Criteria(600, "Yunusobod", 2), "uz-latn", CONTACT)
        self.assertTrue(check(reply, k, CONTACT).ok, check(reply, k, CONTACT).reasons)


class AgentTest(unittest.TestCase):
    def test_uc1_normal_course(self):
        reply, blocked = handle(kb(), "2 xonali Yunusobodda 600$ gacha", Criteria(600, "Yunusobod", 2), CONTACT)
        self.assertEqual(blocked, [])
        self.assertIn("[L-005]", reply)
        self.assertIn("[L-003]", reply)

    def test_uc1_checker_rejection_hands_over(self):
        k = KnowledgeBase("x", [Listing("L-001", "Flat with owner Ali", "Chilonzor", 1, 30, 200, owner_name="Ali")])
        reply, blocked = handle(k, "1 xonali Chilonzorda", Criteria(300, "Chilonzor", 1), CONTACT)
        self.assertTrue(blocked)
        self.assertIn(CONTACT, reply)


if __name__ == "__main__":
    unittest.main()
