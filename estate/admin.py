"""Per-instance conversation statistics for the parent service's admin read API.

A conversation is a SESSION, not a message or a send attempt. The parent calls
begin on inbound messages and finish at explicit session closure, or updates the
same session on replies until handover. Repeated turns never add new sessions.
Use persisted epoch seconds for sessions spanning restarts; each pair of
timestamps must use the same clock.
Only an explicit admin action calls close_deal; a handover does not imply a deal.
These classes are storage primitives, not authorization: the parent must verify
admin access and select the authorized instance_id before exposing stats().
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from threading import RLock

from .telemetry import _duration, _finite, _identifier


@dataclass(frozen=True)
class ConversationRecord:
    """Immutable legacy snapshot of one COMPLETED session, never one message."""

    client_id: str
    duration_seconds: float
    handed_over: bool = False
    closed_deal: bool = False

    def __post_init__(self) -> None:
        _identifier(self.client_id, "client_id")
        duration = _finite(self.duration_seconds, "duration_seconds")
        if duration < 0:
            raise ValueError("duration_seconds must be nonnegative")
        if not isinstance(self.handed_over, bool) or not isinstance(self.closed_deal, bool):
            raise ValueError("handed_over and closed_deal must be bools")
        object.__setattr__(self, "duration_seconds", duration)


@dataclass(frozen=True)
class InstanceStats:
    clients_served: int
    avg_conversation_seconds: float
    handovers: int
    closed_deals: int


class StatsLog:
    """Persistent session lifecycle log; None selects an isolated in-memory DB.

    Session keys are (instance_id, session_id); the parent must namespace session
    IDs across channels within a tenant. Clients served counts distinct
    (instance_id, channel, client_id), including open sessions. Average duration
    includes sessions with at least one finish observation: first inbound to
    latest finish, not the mean response time. For an active session that span is
    provisional; at handover it becomes final. A parent logging only closed
    sessions may call finish just once. Repeated sessions affect the average but
    not client count.

    begin replays retain the FIRST start, even when called at a later turn;
    conflicting channel/client reuse raises ValueError. finish advances the end
    monotonically; old/duplicate observations cannot shorten a session. A
    handed_over=True finish freezes the span, counting exactly one handover;
    all later valid finishes are no-ops. Use a new session ID after handover. Unknown
    sessions raise ValueError. close_deal explicitly marks one deal per session,
    even if still open; duplicate calls never inflate totals, including on restart.

    record remains append-only for legacy callers that already have completed
    ConversationRecords. Every call adds a completed session (no deduplication is
    possible without a session ID). Its clients use a separate legacy channel
    namespace. Do not mix record and begin/finish for the same real conversation.
    close is safe to repeat. Worker threads may share a log instance.
    """

    def __init__(self, database: str | Path | None = None) -> None:
        self._lock = RLock()
        self._db = sqlite3.connect(
            ":memory:" if database is None else str(database), check_same_thread=False
        )
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    instance_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    duration_seconds REAL,
                    handed_over INTEGER NOT NULL DEFAULT 0 CHECK (handed_over IN (0, 1)),
                    closed_deal INTEGER NOT NULL DEFAULT 0 CHECK (closed_deal IN (0, 1)),
                    PRIMARY KEY (instance_id, session_id)
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS conversation_records (
                    id INTEGER PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    duration_seconds REAL NOT NULL CHECK (duration_seconds >= 0),
                    handed_over INTEGER NOT NULL CHECK (handed_over IN (0, 1)),
                    closed_deal INTEGER NOT NULL CHECK (closed_deal IN (0, 1))
                )
            """)
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS conversation_records_instance "
                "ON conversation_records (instance_id)"
            )

    def record(self, instance_id: str, rec: ConversationRecord) -> None:
        _identifier(instance_id, "instance_id")
        if not isinstance(rec, ConversationRecord):
            raise TypeError("rec must be a ConversationRecord")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO conversation_records "
                "(instance_id, client_id, duration_seconds, handed_over, closed_deal) "
                "VALUES (?, ?, ?, ?, ?)",
                (instance_id, rec.client_id, rec.duration_seconds, int(rec.handed_over), int(rec.closed_deal)),
            )

    def begin(
        self, instance_id: str, channel: str, client_id: str, session_id: str, at: float
    ) -> None:
        _identifier(instance_id, "instance_id")
        _identifier(channel, "channel")
        _identifier(client_id, "client_id")
        _identifier(session_id, "session_id")
        at = _finite(at, "at")
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO conversation_sessions "
                "(instance_id, session_id, channel, client_id, started_at) VALUES (?, ?, ?, ?, ?)",
                (instance_id, session_id, channel, client_id, at),
            )
            existing = self._db.execute(
                "SELECT channel, client_id FROM conversation_sessions "
                "WHERE instance_id = ? AND session_id = ?", (instance_id, session_id)
            ).fetchone()
            if existing != (channel, client_id):
                raise ValueError("session replay must retain its original channel and client_id")

    def finish(
        self, instance_id: str, session_id: str, at: float, handed_over: bool = False
    ) -> None:
        _identifier(instance_id, "instance_id")
        _identifier(session_id, "session_id")
        at = _finite(at, "at")
        if not isinstance(handed_over, bool):
            raise ValueError("handed_over must be a bool")
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT started_at FROM conversation_sessions WHERE instance_id = ? AND session_id = ?",
                (instance_id, session_id),
            ).fetchone()
            if row is None:
                raise ValueError("unknown session; call begin first")
            duration = _duration(row[0], at)
            self._db.execute(
                "UPDATE conversation_sessions SET "
                "duration_seconds = CASE WHEN finished_at IS NULL OR finished_at < ? "
                "THEN ? ELSE duration_seconds END, "
                "finished_at = CASE WHEN finished_at IS NULL OR finished_at < ? "
                "THEN ? ELSE finished_at END, handed_over = ? "
                "WHERE instance_id = ? AND session_id = ? AND handed_over = 0",
                (at, duration, at, at, int(handed_over), instance_id, session_id),
            )

    def close_deal(self, instance_id: str, session_id: str) -> None:
        """Explicit admin confirmation: at most one deal for an existing session."""
        _identifier(instance_id, "instance_id")
        _identifier(session_id, "session_id")
        with self._lock, self._db:
            changed = self._db.execute(
                "UPDATE conversation_sessions SET closed_deal = 1 WHERE instance_id = ? AND session_id = ?",
                (instance_id, session_id),
            )
            if changed.rowcount == 0:
                raise ValueError("unknown session; call begin first")

    def stats(self, instance_id: str) -> InstanceStats:
        _identifier(instance_id, "instance_id")
        with self._lock:
            # One query gives a consistent snapshot even with other connections.
            rows = self._db.execute(
                "SELECT channel, client_id, duration_seconds, handed_over, closed_deal "
                "FROM conversation_sessions WHERE instance_id = ? "
                "UNION ALL SELECT '', client_id, duration_seconds, handed_over, closed_deal "
                "FROM conversation_records WHERE instance_id = ?",
                (instance_id, instance_id),
            ).fetchall()
        durations = [row[2] for row in rows if row[2] is not None]
        return InstanceStats(
            clients_served=len({(row[0], row[1]) for row in rows}),
            avg_conversation_seconds=mean(durations) if durations else 0.0,
            handovers=sum(row[3] for row in rows),
            closed_deals=sum(row[4] for row in rows),
        )

    def sessions(self, instance_id: str) -> list[dict]:
        """Detached tenant-scoped snapshots for explicit admin deal selection.

        Intended for the small demo, not an unbounded production admin feed.
        Legacy record() snapshots are excluded: they have no stable session IDs.
        Parent authentication/authorization is required before exposing this list.
        IDs should be opaque synthetic/platform IDs, never client names/phones.
        """
        _identifier(instance_id, "instance_id")
        with self._lock:
            rows = self._db.execute(
                "SELECT session_id, channel, client_id, started_at, finished_at, "
                "duration_seconds, handed_over, closed_deal FROM conversation_sessions "
                "WHERE instance_id = ? ORDER BY started_at, session_id",
                (instance_id,),
            ).fetchall()
        return [
            {
                "session_id": row[0],
                "channel": row[1],
                "client_id": row[2],
                "started_at": row[3],
                "finished_at": row[4],
                "duration_seconds": row[5],
                "handed_over": bool(row[6]),
                "closed_deal": bool(row[7]),
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._db.close()
