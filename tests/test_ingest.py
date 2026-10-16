import unittest

from estate.ingest import is_complete, parse_post
from estate.kb import KnowledgeBase

UZ_POST = """Yunusobod 4-kvartal, 2 xonali kvartira ijaraga beriladi
Maydoni 62 m2, 7/16 qavat, lift bor, parkovka, konditsioner
Narxi: 550$ oyiga"""

RU_POST = """Сдаётся 3-комн квартира, Чиланзар 9 квартал
78 кв.м, мебель, стиральная машина, интернет
Цена 600 у.е в месяц"""


class IngestTest(unittest.TestCase):
    def test_f01_uzbek_post(self):
        l = parse_post("L-100", UZ_POST, "https://t.me/estate_demo/200", ["a.jpg"])
        self.assertEqual((l.district, l.rooms, l.area_m2, l.rent_usd), ("Yunusobod", 2, 62.0, 550))
        self.assertEqual(set(l.amenities), {"elevator", "parking", "air conditioning"})
        self.assertEqual(l.post_url, "https://t.me/estate_demo/200")
        self.assertTrue(is_complete(l))

    def test_f01_russian_post(self):
        l = parse_post("L-101", RU_POST)
        self.assertEqual((l.district, l.rooms, l.area_m2, l.rent_usd), ("Chilonzor", 3, 78.0, 600))
        self.assertIn("furnished", l.amenities)
        self.assertIn("wifi", l.amenities)

    def test_incomplete_post_is_flagged_not_guessed(self):
        l = parse_post("L-102", "Zo'r kvartira, tez orada!")
        self.assertEqual((l.rent_usd, l.district, l.rooms), (0, "", 0))
        self.assertFalse(is_complete(l))

    def test_parsed_post_enters_kb_and_is_offered(self):
        kb = KnowledgeBase("demo")
        kb.add(parse_post("L-100", UZ_POST))
        self.assertEqual([l.id for l in kb.available()], ["L-100"])


if __name__ == "__main__":
    unittest.main()
