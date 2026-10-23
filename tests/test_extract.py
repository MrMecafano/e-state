import unittest

from estate.agent import handle
from estate.extract import extract
from estate.kb import KnowledgeBase


class ExtractTest(unittest.TestCase):
    def test_uzbek_latin_full(self):
        c = extract("Yunusobodda 2 xonali kvartira kerak, 600$ gacha")
        self.assertEqual((c.budget_usd, c.district, c.rooms), (600, "Yunusobod", 2))

    def test_uzbek_cyrillic_words(self):
        c = extract("Чилонзорда икки хонали уй керак")
        self.assertEqual((c.budget_usd, c.district, c.rooms), (None, "Chilonzor", 2))
        self.assertEqual(c.missing(), ["budget_usd"])

    def test_russian(self):
        c = extract("Ищу двухкомнатную в Юнусабаде до 550 долларов")
        self.assertEqual((c.budget_usd, c.district, c.rooms), (550, "Yunusobod", 2))

    def test_nothing_stated_stays_empty(self):
        self.assertEqual(extract("Salom, kvartira bormi?").missing(), ["budget_usd", "district", "rooms"])

    def test_end_to_end_with_agent(self):
        msg = "Yunusobodda 2 xonali kvartira kerak, 600$ gacha"
        reply, blocked = handle(KnowledgeBase.from_json("demo"), msg, extract(msg), "@demo_realtor")
        self.assertEqual(blocked, [])
        self.assertIn("[L-005]", reply)


class ReviewFixes(unittest.TestCase):
    def test_line_above_budget_does_not_merge(self):
        c = extract("Salom\nUylar sonim: 2\n400$ gacha kerak")
        self.assertEqual(c.budget_usd, 400)

    def test_thousands_separator_still_works(self):
        self.assertEqual(extract("1 200$ gacha, 2 xonali").budget_usd, 1200)


if __name__ == "__main__":
    unittest.main()
