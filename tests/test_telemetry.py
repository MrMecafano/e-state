import contextlib
import io
import json
import math
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from estate import agent
from estate.admin import StatsLog
from estate.kb import KnowledgeBase
from estate.matcher import Criteria
from estate.telemetry import LatencyLog
from tools import benchmark_latency as benchmark


class LatencyLogTest(unittest.TestCase):
    def setUp(self):
        self.log = LatencyLog()
        self.addCleanup(self.log.close)

    def test_empty_means_no_measurement_not_a_fast_reply(self):
        self.assertEqual(self.log.summary("tenant"), {
            "count": 0, "failed": 0, "p95_seconds": None, "under_60_seconds": 0,
        })

    def test_nearest_rank_p95_includes_injected_over_60_second_samples(self):
        # Synthetic timestamps, not fake sleeps or a claim about production.
        for index in range(100):
            self.log.record("tenant", "telegram", str(index), 1000, 1001 if index < 94 else 1061)
        self.assertEqual(self.log.summary("tenant"), {
            "count": 100, "failed": 0, "p95_seconds": 61, "under_60_seconds": 94,
        })

    def test_nearest_rank_boundary_is_not_interpolated(self):
        for index in range(100):
            self.log.record("tenant", "telegram", str(index), 0, index + 1)
        self.assertEqual(self.log.summary("tenant")["p95_seconds"], 95)
        for index, duration in enumerate((1, 2, 100)):
            self.log.record("small", "telegram", str(index), 0, duration)
        self.assertEqual(self.log.summary("small")["p95_seconds"], 100)

    def test_under_60_is_strict_and_zero_is_valid(self):
        for index, duration in enumerate((0, 59.99, 60, 61)):
            self.log.record("tenant", "telegram", str(index), 0, duration)
        self.assertEqual(self.log.summary("tenant")["under_60_seconds"], 2)

    def test_errors_are_not_successes_and_are_deduplicated(self):
        for _ in range(3):
            self.log.record("tenant", "telegram", "failed", 0, 120, success=False)
        self.assertEqual(self.log.summary("tenant"), {
            "count": 0, "failed": 1, "p95_seconds": None, "under_60_seconds": 0,
        })
        self.log.record("tenant", "telegram", "success", 0, 1)
        self.assertEqual(self.log.summary("tenant"), {
            "count": 1, "failed": 1, "p95_seconds": 1, "under_60_seconds": 1,
        })

    def test_retry_ack_uses_original_receipt_and_freezes_first_success(self):
        self.log.record("tenant", "telegram", "message", 100, 110, success=False)
        self.log.record("tenant", "telegram", "message", 150, 170, success=True)
        self.log.record("tenant", "telegram", "message", 200, 220, success=True)
        self.log.record("tenant", "telegram", "message", 230, 240, success=False)
        self.assertEqual(self.log.summary("tenant"), {
            "count": 1, "failed": 0, "p95_seconds": 70, "under_60_seconds": 0,
        })

    def test_repeated_first_replies_count_once(self):
        self.log.record("tenant", "telegram", "message", 100, 102)
        self.log.record("tenant", "telegram", "message", 100, 150)
        self.assertEqual(self.log.summary("tenant")["count"], 1)
        self.assertEqual(self.log.summary("tenant")["p95_seconds"], 2)

    def test_tenant_and_channel_namespaces_do_not_collide(self):
        self.log.record("a", "telegram:chat-1", "same-id", 0, 1)
        self.log.record("a", "telegram:chat-2", "same-id", 0, 3)
        self.log.record("a", "instagram", "same-id", 0, 7, success=False)
        self.log.record("b", "telegram:chat-1", "same-id", 0, 120)
        self.assertEqual(self.log.summary("a"), {
            "count": 2, "failed": 1, "p95_seconds": 3, "under_60_seconds": 2,
        })
        self.assertEqual(self.log.summary("b"), {
            "count": 1, "failed": 0, "p95_seconds": 120, "under_60_seconds": 0,
        })

    def test_invalid_times_are_rejected_before_mutation(self):
        pairs = [(10, 9), (-1e308, 1e308)]
        for invalid in (math.nan, math.inf, -math.inf, True, "1", None, 10**1000):
            pairs.extend(((invalid, 1), (0, invalid)))
        for start, end in pairs:
            with self.subTest(start=repr(start), end=repr(end)), self.assertRaises(ValueError):
                self.log.record("tenant", "telegram", "message", start, end)
        self.assertEqual(self.log.summary("tenant")["count"], 0)
        self.assertEqual(self.log.summary("tenant")["failed"], 0)

    def test_retry_timestamp_cannot_precede_original_receipt(self):
        self.log.record("tenant", "telegram", "message", 100, 110, success=False)
        with self.assertRaises(ValueError):
            self.log.record("tenant", "telegram", "message", 0, 10)
        self.assertEqual(self.log.summary("tenant")["failed"], 1)
        self.assertEqual(self.log.summary("tenant")["count"], 0)

    def test_explicit_bool_and_nonempty_identifiers_required(self):
        for success in (0, 1, "false", None):
            with self.subTest(success=success), self.assertRaises(ValueError):
                self.log.record("tenant", "telegram", "message", 0, 1, success=success)
        for index in range(3):
            args = ["tenant", "telegram", "message", 0, 1]
            args[index] = " "
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.log.record(*args)

    def test_memory_instances_are_independent(self):
        self.log.record("tenant", "telegram", "message", 0, 1)
        other = LatencyLog()
        self.addCleanup(other.close)
        self.assertEqual(other.summary("tenant")["count"], 0)

    def test_parent_worker_threads_share_first_ack_without_duplicate_counts(self):
        def record(_):
            self.log.record("tenant", "telegram", "message", 0, 1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(record, range(20)))
        self.assertEqual(self.log.summary("tenant")["count"], 1)

    def test_parent_ack_boundary_and_delivery_error_contract(self):
        # A ready reply is not delivery. This example parent path records only
        # after the sender returns its ACK, and records exceptions as failures.
        reply, _ = agent.handle(KnowledgeBase("tenant"), "kvartira kerak", Criteria(), "@synthetic_realtor")
        self.assertTrue(reply)
        self.assertEqual(self.log.summary("tenant")["count"], 0)

        def sender(fail):
            self.assertEqual(self.log.summary("tenant")["count"], 0)
            if fail:
                raise OSError("synthetic delivery error")
            return {"ack": True}

        for fail, sent_at in ((True, 110), (False, 170)):
            try:
                ack = sender(fail)
            except OSError:
                self.log.record("tenant", "telegram", "message", 100, sent_at, success=False)
            else:
                self.assertTrue(ack["ack"])
                self.log.record("tenant", "telegram", "message", 100, sent_at)
        self.assertEqual(self.log.summary("tenant")["p95_seconds"], 70)
        self.assertEqual(self.log.summary("tenant")["failed"], 0)


class PersistentLatencyTest(unittest.TestCase):
    def test_restart_keeps_failures_first_receipts_and_first_acks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.sqlite3"
            first = LatencyLog(path)
            first.record("a", "telegram", "reply", 100, 102)
            first.record("a", "telegram", "retry", 100, 110, success=False)
            first.record("b", "telegram", "reply", 100, 220)
            first.close()
            first.close()
            second = LatencyLog(str(path))
            self.addCleanup(second.close)
            self.assertEqual(second.summary("a"), {
                "count": 1, "failed": 1, "p95_seconds": 2, "under_60_seconds": 1,
            })
            second.record("a", "telegram", "reply", 100, 300)
            second.record("a", "telegram", "retry", 150, 170)
            self.assertEqual(second.summary("a"), {
                "count": 2, "failed": 0, "p95_seconds": 70, "under_60_seconds": 1,
            })
            self.assertEqual(second.summary("b")["p95_seconds"], 120)

    def test_logs_can_share_database_and_independent_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.sqlite3"
            stats, first, second = StatsLog(path), LatencyLog(path), LatencyLog(path)
            for log in (stats, first, second):
                self.addCleanup(log.close)
            stats.begin("tenant", "telegram", "c1", "s1", 0)
            first.record("tenant", "telegram", "message", 0, 1, success=False)
            second.record("tenant", "telegram", "message", 10, 70)
            first.record("tenant", "telegram", "message", 0, 80)
            self.assertEqual(first.summary("tenant")["p95_seconds"], 70)
            self.assertEqual(second.summary("tenant")["count"], 1)
            self.assertEqual(stats.stats("tenant").clients_served, 1)


class OfflineBenchmarkTest(unittest.TestCase):
    def test_actual_handler_gets_100_distinct_messages_without_network(self):
        actual_handle = agent.handle
        with patch.object(benchmark.agent, "handle", wraps=actual_handle) as handle, \
                patch("socket.socket", side_effect=AssertionError("network forbidden")):
            report = benchmark.run_benchmark()
        self.assertEqual(handle.call_count, 100)
        self.assertEqual(len({call.args[1] for call in handle.call_args_list}), 100)
        self.assertEqual(report["count"], 100)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(len(report["samples_seconds"]), 100)
        self.assertTrue(all(math.isfinite(value) and value >= 0 for value in report["samples_seconds"]))
        self.assertEqual(report["p95_seconds"], sorted(report["samples_seconds"])[94])
        self.assertEqual(report["scope"], "offline_local_only")
        self.assertEqual(report["live_api_calls"], 0)
        self.assertEqual(report["api_delivery_success_count"], 0)
        self.assertIn("24/7 availability is not proven.", report["limitations"])

    def test_injected_slow_p95_makes_cli_fail_without_waiting(self):
        times = []
        for index in range(100):
            start = index * 1000
            times.extend((start, start + (1 if index < 94 else 61)))
        output = io.StringIO()
        with patch.object(benchmark, "perf_counter", side_effect=times), contextlib.redirect_stdout(output):
            exit_code = benchmark.main([])
        report = json.loads(output.getvalue())
        self.assertEqual(report["count"], 100)
        self.assertEqual(report["p95_seconds"], 61)
        self.assertEqual(report["under_60_seconds"], 94)
        self.assertFalse(report["passed"])
        self.assertEqual(exit_code, 1)

    def test_exact_60_second_p95_also_fails(self):
        times = [value for i in range(100) for value in (i * 100, i * 100 + 60)]
        with patch.object(benchmark, "perf_counter", side_effect=times):
            report = benchmark.run_benchmark()
        self.assertEqual(report["p95_seconds"], 60)
        self.assertFalse(report["passed"])

    def test_cli_fails_with_fewer_than_100_or_no_messages(self):
        for count in (99, 0):
            output = io.StringIO()
            with self.subTest(count=count), contextlib.redirect_stdout(output):
                exit_code = benchmark.main(["--messages", str(count)])
            report = json.loads(output.getvalue())
            self.assertEqual(report["count"], count)
            self.assertEqual(exit_code, 1)
            self.assertFalse(report["passed"])

    def test_processing_errors_never_count_as_successful_replies(self):
        actual_handle = agent.handle
        calls = 0

        def handler(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic handler error")
            return actual_handle(*args)

        output = io.StringIO()
        with patch.object(benchmark.agent, "handle", side_effect=handler), contextlib.redirect_stdout(output):
            exit_code = benchmark.main([])
        report = json.loads(output.getvalue())
        self.assertEqual(calls, 100)
        self.assertEqual(report["count"], 99)
        self.assertEqual(report["failed"], 1)
        self.assertFalse(report["passed"])
        self.assertEqual(exit_code, 1)

    def test_direct_script_invocation_prints_honest_json_from_any_cwd(self):
        script = Path(benchmark.__file__).resolve()
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(script)], cwd=directory, capture_output=True, text=True, check=False
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["scope"], "offline_local_only")
        self.assertEqual(report["count"], 100)
        self.assertTrue(report["passed"])
        self.assertTrue(any("unconfigured" in value for value in report["limitations"]))


if __name__ == "__main__":
    unittest.main()
