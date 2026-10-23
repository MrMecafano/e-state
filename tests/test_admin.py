import unittest

from estate.admin import ConversationRecord, StatsLog


class StatsLogTest(unittest.TestCase):
    def test_f15_empty_instance_has_zeroed_stats(self):
        s = StatsLog().stats("demo_realtor")
        self.assertEqual((s.clients_served, s.avg_conversation_seconds, s.handovers, s.closed_deals), (0, 0.0, 0, 0))

    def test_f15_aggregates_one_instance(self):
        log = StatsLog()
        log.record("demo_realtor", ConversationRecord("c1", 60.0))
        log.record("demo_realtor", ConversationRecord("c2", 120.0, handed_over=True))
        log.record("demo_realtor", ConversationRecord("c2", 30.0, closed_deal=True))  # same client, 2nd message

        s = log.stats("demo_realtor")
        self.assertEqual(s.clients_served, 2)  # distinct clients, not messages
        self.assertAlmostEqual(s.avg_conversation_seconds, (60.0 + 120.0 + 30.0) / 3)
        self.assertEqual(s.handovers, 1)
        self.assertEqual(s.closed_deals, 1)

    def test_n02_instances_do_not_share_stats(self):
        log = StatsLog()
        log.record("a", ConversationRecord("c1", 60.0, closed_deal=True))
        self.assertEqual(log.stats("b").clients_served, 0)
        self.assertEqual(log.stats("a").closed_deals, 1)


if __name__ == "__main__":
    unittest.main()
