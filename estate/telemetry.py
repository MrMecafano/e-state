"""First-reply delivery latency, separate from conversation duration.

The parent service must capture received_at on ingress and call record(success=True)
ONLY AFTER the channel API acknowledges delivery, with sent_at captured at that ACK.
On processing/delivery errors it may record(success=False) at the failure time. A
returned agent reply, queued send, or handover is not an API acknowledgement.
Use one clock for each timestamp pair (e.g. perf_counter in-process, or persisted
UTC epoch timestamps across restarts); never mix epoch/monotonic timestamps or
persist monotonic values across different clock domains.
No message text, credentials, or client personal data is stored here.
"""
from __future__ import annotations

import math
import sqlite3
from numbers import Real
from pathlib import Path
from threading import RLock


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _duration(start: float, end: float) -> float:
    result = _finite(end - start, "duration_seconds")
    if result < 0:
        raise ValueError("duration_seconds must be nonnegative")
    return result


class LatencyLog:
    """SQLite-backed message outcomes; None selects an isolated in-memory DB.

    Keys are (instance_id, channel, message_id). The channel must identify the
    namespace in which message IDs are unique (include a chat/account ID when the
    platform only guarantees chat-local IDs). Use opaque IDs, not names/phones.
    The FIRST received_at is retained automatically across retries, even if the
    parent supplies a later HTTP receipt time; retries cannot reset the clock.

    Failures are deduplicated and can be upgraded by a later successful ACK.
    After success, failures and subsequent replies cannot replace the first ACK.
    summary counts distinct successfully answered messages, and failed counts
    distinct messages still lacking a successful ACK, not failed send attempts.
    p95_seconds is nearest-rank ceil(.95 * count), or None with no successes.
    under_60_seconds counts successful first replies strictly below 60 seconds.
    Persistence is opt-in via a file path; close releases the connection, safely
    more than once. Instances may be used by parent-service worker threads.
    """

    def __init__(self, database: str | Path | None = None) -> None:
        self._lock = RLock()
        self._db = sqlite3.connect(
            ":memory:" if database is None else str(database), check_same_thread=False
        )
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS reply_latency (
                    instance_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    sent_at REAL NOT NULL,
                    success INTEGER NOT NULL CHECK (success IN (0, 1)),
                    PRIMARY KEY (instance_id, channel, message_id)
                )
            """)

    def record(
        self,
        instance_id: str,
        channel: str,
        message_id: str,
        received_at: float,
        sent_at: float,
        success: bool = True,
    ) -> None:
        key = (
            _identifier(instance_id, "instance_id"),
            _identifier(channel, "channel"),
            _identifier(message_id, "message_id"),
        )
        received_at = _finite(received_at, "received_at")
        sent_at = _finite(sent_at, "sent_at")
        _duration(received_at, sent_at)
        if not isinstance(success, bool):
            raise ValueError("success must be a bool")
        with self._lock, self._db:
            # Insert then read inside the same write transaction, so independent
            # connections cannot overwrite an ACK or reset the ingress timestamp.
            self._db.execute(
                "INSERT OR IGNORE INTO reply_latency VALUES (?, ?, ?, ?, ?, ?)",
                (*key, received_at, sent_at, int(success)),
            )
            original_received_at = self._db.execute(
                "SELECT received_at FROM reply_latency "
                "WHERE instance_id = ? AND channel = ? AND message_id = ?", key
            ).fetchone()[0]
            _duration(original_received_at, sent_at)
            if success:
                self._db.execute(
                    "UPDATE reply_latency SET sent_at = ?, success = 1 "
                    "WHERE instance_id = ? AND channel = ? AND message_id = ? AND success = 0",
                    (sent_at, *key),
                )

    def summary(self, instance_id: str) -> dict:
        _identifier(instance_id, "instance_id")
        with self._lock:
            rows = self._db.execute(
                "SELECT received_at, sent_at, success FROM reply_latency WHERE instance_id = ?",
                (instance_id,),
            ).fetchall()
        durations = sorted(end - start for start, end, success in rows if success)
        count = len(durations)
        return {
            "count": count,
            "failed": len(rows) - count,
            "p95_seconds": durations[(95 * count + 99) // 100 - 1] if count else None,
            "under_60_seconds": sum(value < 60 for value in durations),
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()
