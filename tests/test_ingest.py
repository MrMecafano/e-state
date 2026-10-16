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



class ReviewFixes(unittest.TestCase):
    def test_line_above_price_does_not_merge(self):
        l = parse_post("X-1", "Yunusobod, 2 xonali\nQavat: 4\n450$")
        self.assertEqual(l.rent_usd, 450)

    def test_thousands_separator_still_works(self):
        self.assertEqual(parse_post("X-2", "Chilonzor 3 xonali 1 200 $").rent_usd, 1200)

    def test_unfurnished_is_not_furnished(self):
        self.assertNotIn("furnished", parse_post("X-3", "Sergeli 1 xonali mebelsiz 250$").amenities)
        self.assertNotIn("furnished", parse_post("X-4", "Сергели 1-комн без мебели 250$").amenities)
        self.assertIn("furnished", parse_post("X-5", "Sergeli 1 xonali mebel bor 250$").amenities)

    def test_earliest_district_wins(self):
        self.assertEqual(parse_post("X-6", "Uchtepa, Chilonzor chegarasida, 2 xonali 400$").district, "Uchtepa")

if __name__ == "__main__":
    unittest.main()
