#!/usr/bin/env python3
"""Reproducible, serial, synthetic 100-message LOCAL processing benchmark.

Run from any directory: python /path/to/tools/benchmark_latency.py
No sleeps, credentials, fixture personal data, network, or channel API calls.
Times ingress text -> extraction -> estate.agent.handle -> checked reply ready
using perf_counter. These are NOT acknowledged delivery measurements and are not
written to LatencyLog. Telegram/Instagram latency, unconfigured APIs and 24/7
availability are not proven by this benchmark. Hardware/load affect timings.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path
from time import perf_counter

# Support direct invocation without installing a package or changing the CWD.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from estate import agent
from estate.extract import extract
from estate.kb import KnowledgeBase, Listing


_MESSAGES = (
    "2 xonali Yunusobodda 600$ gacha",
    "Нужна 1 комнатная квартира в Чиланзаре за 400$",
    "kvartira kerak",
    "Uyni ko'rsam bo'ladimi?",
    "Oldindan to'lov qilishim kerakmi?",
)


def run_benchmark(message_count: int = 100) -> dict:
    """Actually drive handle once per distinct synthetic message, without I/O."""
    if isinstance(message_count, bool) or not isinstance(message_count, int) or message_count < 0:
        raise ValueError("message_count must be a nonnegative integer")
    kb = KnowledgeBase("offline-benchmark", [
        Listing("L-001", "Synthetic two-room flat", "Yunusobod", 2, 55, 500),
        Listing("L-002", "Synthetic one-room flat", "Chilonzor", 1, 30, 300),
    ])
    durations = []
    failed = 0
    for index in range(message_count):
        message = f"{_MESSAGES[index % len(_MESSAGES)]} (synthetic request {index:04d})"
        received_at = perf_counter()
        try:
            reply, _reasons = agent.handle(kb, message, extract(message), "@synthetic_realtor")
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("no local reply produced")
        except Exception:
            # A failed handler invocation is not a successful first reply. Do not
            # print exception text, which could contain future credentials/data.
            failed += 1
        else:
            durations.append(perf_counter() - received_at)
    ordered = sorted(durations)
    count = len(ordered)
    p95 = ordered[(95 * count + 99) // 100 - 1] if count else None
    passed = count >= 100 and failed == 0 and p95 is not None and p95 < 60
    return {
        "scope": "offline_local_only",
        "workload": "synthetic_v1_five_paths_serial",
        "clock": "perf_counter",
        "python_version": platform.python_version(),
        "requested_messages": message_count,
        "count": count,
        "failed": failed,
        "p95_seconds": p95,
        "quantile": "nearest_rank",
        "under_60_seconds": sum(value < 60 for value in ordered),
        "samples_seconds": durations,
        "passed": passed,
        "live_api_calls": 0,
        "api_delivery_success_count": 0,
        "limitations": [
            "Only end-to-end local processing to a checked reply is measured; no API ACK.",
            "Telegram and Instagram APIs are unconfigured and not proven.",
            "24/7 availability is not proven.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=100, help="default 100; fewer than 100 fails")
    args = parser.parse_args(argv)
    if args.messages < 0:
        parser.error("--messages must be nonnegative")
    report = run_benchmark(args.messages)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
