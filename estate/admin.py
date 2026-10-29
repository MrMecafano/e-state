"""Per-instance admin statistics (F-15): clients served, average conversation
time, number of handovers and number of closed deals.

Nothing else in this codebase logs conversations yet, so this module owns a
minimal append-only log the agent (or, for now, a test/demo script) writes one
ConversationRecord to per finished conversation. The admin panel reads stats()
for one realtor instance at a time -- one instance's log is never mixed into
another's totals (N-02).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ConversationRecord:
    client_id: str
    duration_seconds: float
    handed_over: bool = False
    closed_deal: bool = False


@dataclass
class InstanceStats:
    clients_served: int
    avg_conversation_seconds: float
    handovers: int
    closed_deals: int


class StatsLog:
    """Append-only per-instance conversation log (F-15)."""

    def __init__(self) -> None:
        self._records: dict[str, list[ConversationRecord]] = {}

    def record(self, instance_id: str, rec: ConversationRecord) -> None:
        self._records.setdefault(instance_id, []).append(rec)

    def stats(self, instance_id: str) -> InstanceStats:
        records = self._records.get(instance_id, [])
        if not records:
            return InstanceStats(clients_served=0, avg_conversation_seconds=0.0, handovers=0, closed_deals=0)
        clients = {r.client_id for r in records}
        return InstanceStats(
            clients_served=len(clients),
            avg_conversation_seconds=sum(r.duration_seconds for r in records) / len(records),
            handovers=sum(1 for r in records if r.handed_over),
            closed_deals=sum(1 for r in records if r.closed_deal),
        )
