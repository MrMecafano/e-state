"""Parent-callable session lifecycle contract, without a live service/API."""
import math
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

from estate import agent
from estate.admin import ConversationRecord, InstanceStats, StatsLog
from estate.kb import KnowledgeBase
from estate.matcher import Criteria


class ConversationRecordTest(unittest.TestCase):
    def test_record_and_stats_snapshots_are_immutable(self):
        rec = ConversationRecord("synthetic-client", 12)
        with self.assertRaises(FrozenInstanceError):
            rec.duration_seconds = 99
        with self.assertRaises(FrozenInstanceError):
            rec.client_id = "replacement"
        log = StatsLog()
        self.addCleanup(log.close)
        log.record("tenant", rec)
        snapshot = log.stats("tenant")
        with self.assertRaises(FrozenInstanceError):
            snapshot.closed_deals = 1
        self.assertEqual(log.stats("tenant"), InstanceStats(1, 12, 0, 0))

    def test_finite_nonnegative_durations_required(self):
        for value in (-1, math.nan, math.inf, -math.inf, True, "10", None, 10**1000):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                ConversationRecord("synthetic-client", value)
        self.assertEqual(ConversationRecord("synthetic-client", 0).duration_seconds, 0)

    def test_record_flags_must_be_explicit_bools(self):
        for kwargs in ({"handed_over": "yes"}, {"closed_deal": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ConversationRecord("synthetic-client", 1, **kwargs)


class SessionLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.log = StatsLog()
        self.addCleanup(self.log.close)

    def test_empty_and_in_memory_logs_are_isolated(self):
        self.assertEqual(self.log.stats("tenant"), InstanceStats(0, 0, 0, 0))
        self.log.begin("tenant", "telegram", "synthetic-client", "s1", 100)
        other = StatsLog()
        self.addCleanup(other.close)
        self.assertEqual(other.stats("tenant"), InstanceStats(0, 0, 0, 0))

    def test_clients_include_open_sessions_but_average_requires_finish_observation(self):
        self.log.begin("tenant", "telegram", "c1", "s1", 100)
        self.assertEqual(self.log.stats("tenant"), InstanceStats(1, 0, 0, 0))
        self.log.begin("tenant", "telegram", "c2", "s2", 200)
        self.log.finish("tenant", "s1", 160)
        self.assertEqual(self.log.stats("tenant"), InstanceStats(2, 60, 0, 0))
        self.log.finish("tenant", "s2", 320, handed_over=True)
        self.assertEqual(self.log.stats("tenant"), InstanceStats(2, 90, 1, 0))

    def test_multiple_turns_are_one_conversation_not_response_times(self):
        self.log.begin("tenant", "telegram", "synthetic-client", "s1", 1000)
        kb = KnowledgeBase("tenant")
        for message in ("kvartira kerak", "2 xonali", "Yunusobod 600$"):
            reply, _ = agent.handle(kb, message, Criteria(), "@synthetic_realtor")
            self.assertTrue(reply)
        # The parent closes only at session closure, not after each handle call.
        self.log.finish("tenant", "s1", 1180)
        self.log.begin("tenant", "telegram", "synthetic-client", "s2", 2000)
        self.log.finish("tenant", "s2", 2060)
        self.assertEqual(self.log.stats("tenant"), InstanceStats(1, 120, 0, 0))

    def test_begin_replay_is_idempotent_even_after_finish(self):
        for at in (10, 15, 18):
            self.log.begin("tenant", "telegram", "c1", "s1", at)
        self.log.finish("tenant", "s1", 20)
        self.log.begin("tenant", "telegram", "c1", "s1", 99)
        self.assertEqual(self.log.stats("tenant"), InstanceStats(1, 10, 0, 0))

    def test_conflicting_session_reuse_is_rejected_without_mutation(self):
        self.log.begin("tenant", "telegram", "c1", "s1", 10)
        for channel, client, at in (("instagram", "c1", 10), ("telegram", "c2", 10)):
            with self.subTest(channel=channel, client=client, at=at), self.assertRaises(ValueError):
                self.log.begin("tenant", channel, client, "s1", at)
        self.log.finish("tenant", "s1", 30)
        self.assertEqual(self.log.stats("tenant"), InstanceStats(1, 20, 0, 0))

    def test_finish_updates_monotonically_until_handover_then_freezes(self):
        self.log.begin("tenant", "telegram", "c1", "s1", 100)
        self.log.finish("tenant", "s1", 130)
        self.log.begin("tenant", "telegram", "c1", "s1", 180)
        self.log.finish("tenant", "s1", 220)
        self.log.finish("tenant", "s1", 130)  # stale turn cannot shorten it
        self.assertEqual(self.log.stats("tenant"), InstanceStats(1, 120, 0, 0))
        self.log.finish("tenant", "s1", 250, handed_over=True)
        self.log.finish("tenant", "s1", 999, handed_over=True)
        self.log.finish("tenant", "s1", 500, handed_over=False)
        self.assertEqual(self.log.stats("tenant"), InstanceStats(1, 150, 1, 0))

    def test_stale_handover_does_not_shorten_latest_session_span(self):
        self.log.begin("tenant", "telegram", "c1", "s1", 100)
        self.log.finish("tenant", "s1", 220)
        self.log.finish("tenant", "s1", 200, handed_over=True)
        self.log.finish("tenant", "s1", 300)
        self.assertEqual(self.log.stats("tenant"), InstanceStats(1, 120, 1, 0))

    def test_deal_closure_is_explicit_and_idempotent(self):
        self.log.begin("tenant", "telegram", "c1", "s1", 10)
        self.log.finish("tenant", "s1", 20, handed_over=True)
        self.assertEqual(self.log.stats("tenant").closed_deals, 0)
        for _ in range(5):
            self.log.close_deal("tenant", "s1")
        self.assertEqual(self.log.stats("tenant"), InstanceStats(1, 10, 1, 1))
        # Admin confirmation is allowed before a session is finished as well.
        self.log.begin("tenant", "telegram", "c1", "s2", 30)
        self.log.close_deal("tenant", "s2")
        self.log.close_deal("tenant", "s2")
        self.assertEqual(self.log.stats("tenant"), InstanceStats(1, 10, 1, 2))

    def test_unknown_and_other_tenant_sessions_cannot_be_closed(self):
        self.log.begin("a", "telegram", "c1", "s1", 0)
        for tenant, session in (("a", "missing"), ("b", "s1")):
            with self.subTest(tenant=tenant), self.assertRaises(ValueError):
                self.log.finish(tenant, session, 1)
            with self.subTest(tenant=tenant), self.assertRaises(ValueError):
                self.log.close_deal(tenant, session)
        self.assertEqual(self.log.stats("b"), InstanceStats(0, 0, 0, 0))

    def test_clients_are_distinct_per_tenant_and_channel(self):
        for tenant in ("a", "b"):
            self.log.begin(tenant, "telegram", "same-id", "tg:s1", 10)
            self.log.begin(tenant, "instagram", "same-id", "ig:s1", 10)
        self.log.finish("a", "tg:s1", 20, handed_over=True)
        self.log.close_deal("a", "tg:s1")
        self.log.finish("a", "ig:s1", 40)
        self.assertEqual(self.log.stats("a"), InstanceStats(2, 20, 1, 1))
        self.assertEqual(self.log.stats("b"), InstanceStats(2, 0, 0, 0))

    def test_invalid_timestamps_do_not_create_or_finish_sessions(self):
        for at in (math.nan, math.inf, -math.inf, "1", True, 10**1000):
            with self.subTest(at=repr(at)), self.assertRaises(ValueError):
                self.log.begin("tenant", "telegram", "c1", "bad", at)
        self.assertEqual(self.log.stats("tenant").clients_served, 0)
        self.log.begin("tenant", "telegram", "c1", "s1", 100)
        for at in (99, math.nan, math.inf, -math.inf, "1", True):
            with self.subTest(at=repr(at)), self.assertRaises(ValueError):
                self.log.finish("tenant", "s1", at)
        self.assertEqual(self.log.stats("tenant").avg_conversation_seconds, 0)
        self.log.finish("tenant", "s1", 100)
        self.assertEqual(self.log.stats("tenant"), InstanceStats(1, 0, 0, 0))

    def test_finite_timestamps_cannot_produce_infinite_duration(self):
        self.log.begin("tenant", "telegram", "c1", "s1", -1e308)
        with self.assertRaises(ValueError):
            self.log.finish("tenant", "s1", 1e308)
        self.log.finish("tenant", "s1", -1e308)
        self.assertEqual(self.log.stats("tenant").avg_conversation_seconds, 0)

    def test_large_valid_durations_have_a_finite_average(self):
        for _ in range(3):
            self.log.record("tenant", ConversationRecord("c1", 1e308))
        self.assertEqual(self.log.stats("tenant").avg_conversation_seconds, 1e308)

    def test_legacy_records_are_append_only_in_a_separate_channel(self):
        rec = ConversationRecord("c1", 10, closed_deal=True)
        self.log.record("tenant", rec)
        self.log.record("tenant", rec)
        self.log.begin("tenant", "telegram", "c1", "s1", 100)
        self.log.finish("tenant", "s1", 140)
        self.assertEqual(self.log.stats("tenant"), InstanceStats(2, 20, 0, 2))

    def test_identifiers_and_handover_are_validated(self):
        for field in range(4):
            args = ["tenant", "telegram", "c1", "s1", 0]
            args[field] = " "
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.log.begin(*args)
        self.log.begin("tenant", "telegram", "c1", "s1", 0)
        with self.assertRaises(ValueError):
            self.log.finish("tenant", "s1", 1, handed_over="yes")

    def test_admin_session_snapshots_are_tenant_scoped_and_detached(self):
        self.log.begin("a", "telegram", "c1", "s2", 200)
        self.log.begin("a", "instagram", "c2", "s1", 100)
        self.log.begin("b", "telegram", "c1", "s1", 1)
        self.log.record("a", ConversationRecord("legacy", 5))
        self.log.finish("a", "s1", 150, handed_over=True)
        self.log.close_deal("a", "s1")
        self.assertEqual(self.log.sessions("missing"), [])
        snapshots = self.log.sessions("a")
        self.assertEqual(snapshots, [
            {"session_id": "s1", "channel": "instagram", "client_id": "c2",
             "started_at": 100, "finished_at": 150, "duration_seconds": 50,
             "handed_over": True, "closed_deal": True},
            {"session_id": "s2", "channel": "telegram", "client_id": "c1",
             "started_at": 200, "finished_at": None, "duration_seconds": None,
             "handed_over": False, "closed_deal": False},
        ])
        snapshots[0]["closed_deal"] = False
        snapshots.clear()
        self.assertTrue(self.log.sessions("a")[0]["closed_deal"])
        self.assertEqual(self.log.sessions("b")[0]["started_at"], 1)

    def test_parent_worker_threads_can_share_the_log(self):
        def work(_):
            self.log.begin("tenant", "telegram", "c1", "s1", 100)
            self.log.finish("tenant", "s1", 160, handed_over=True)
            self.log.close_deal("tenant", "s1")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(work, range(20)))
        self.assertEqual(self.log.stats("tenant"), InstanceStats(1, 60, 1, 1))


class PersistentStatsTest(unittest.TestCase):
    def test_restart_keeps_open_finished_legacy_and_deal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.sqlite3"
            first = StatsLog(path)
            first.begin("tenant", "telegram", "c1", "s1", 100)
            first.begin("tenant", "telegram", "c2", "s2", 200)
            first.finish("tenant", "s1", 160, handed_over=True)
            first.close_deal("tenant", "s1")
            first.record("tenant", ConversationRecord("legacy", 20))
            first.close()
            first.close()

            second = StatsLog(str(path))
            self.addCleanup(second.close)
            self.assertEqual(second.stats("tenant"), InstanceStats(3, 40, 1, 1))
            second.begin("tenant", "telegram", "c1", "s1", 100)
            second.finish("tenant", "s1", 999, handed_over=True)
            second.close_deal("tenant", "s1")
            second.finish("tenant", "s2", 300)
            self.assertEqual(second.stats("tenant"), InstanceStats(3, 60, 1, 1))
            self.assertEqual(second.stats("other"), InstanceStats(0, 0, 0, 0))

    def test_independent_connections_share_committed_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.sqlite3"
            a, b = StatsLog(path), StatsLog(path)
            self.addCleanup(a.close)
            self.addCleanup(b.close)
            a.begin("tenant", "telegram", "c1", "s1", 10)
            b.begin("tenant", "telegram", "c1", "s1", 10)
            b.finish("tenant", "s1", 20, handed_over=True)
            a.finish("tenant", "s1", 100)
            a.close_deal("tenant", "s1")
            b.close_deal("tenant", "s1")
            self.assertEqual(a.stats("tenant"), InstanceStats(1, 10, 1, 1))
            self.assertEqual(b.stats("tenant"), a.stats("tenant"))


if __name__ == "__main__":
    unittest.main()
