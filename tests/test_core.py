import unittest
from datetime import datetime

from realtor_agent.booking import BookingError, Calendar
from realtor_agent.listings import by_id, load_listings
from realtor_agent.matcher import Criteria, match
from realtor_agent.qa import UNKNOWN, answer


class MatcherTest(unittest.TestCase):
    def setUp(self):
        self.listings = load_listings()

    def test_budget_and_district_filter(self):
        hits = match(self.listings, Criteria(budget_usd=80000, district="Yunusobod", rooms=2))
        self.assertEqual([l.id for l, _ in hits], ["L-003"])  # L-006 is over budget

    def test_must_have_parking(self):
        hits = match(self.listings, Criteria(budget_usd=100000, must_have=["parking"]))
        self.assertTrue(hits)
        self.assertTrue(all(l.parking for l, _ in hits))

    def test_no_match_returns_empty(self):
        self.assertEqual(match(self.listings, Criteria(budget_usd=10000)), [])


class QATest(unittest.TestCase):
    def setUp(self):
        self.l3 = by_id(load_listings(), "L-003")

    def test_known_facts(self):
        self.assertIn("parking is included", answer(self.l3, "Is there parking?"))
        self.assertIn("78,000", answer(self.l3, "How much is it?"))

    def test_unknown_is_not_guessed(self):
        self.assertEqual(answer(self.l3, "Is the neighbour noisy?"), UNKNOWN)


class BookingTest(unittest.TestCase):
    def test_double_booking_rejected(self):
        cal = Calendar()
        cal.book("L-003", "Aziz", datetime(2026, 10, 20, 15, 0))
        with self.assertRaises(BookingError):
            cal.book("L-003", "Bobur", datetime(2026, 10, 20, 15, 30))

    def test_other_listing_same_time_ok(self):
        cal = Calendar()
        cal.book("L-003", "Aziz", datetime(2026, 10, 20, 15, 0))
        cal.book("L-004", "Bobur", datetime(2026, 10, 20, 15, 0))

    def test_outside_hours_and_sunday(self):
        cal = Calendar()
        with self.assertRaises(BookingError):
            cal.book("L-003", "Aziz", datetime(2026, 10, 20, 18, 30))
        with self.assertRaises(BookingError):
            cal.book("L-003", "Aziz", datetime(2026, 10, 18, 11, 0))  # Sunday


if __name__ == "__main__":
    unittest.main()
