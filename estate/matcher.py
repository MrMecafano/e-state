"""Recommend up to 3 available listings; ask for missing criteria (F-03)."""
from __future__ import annotations

from dataclasses import dataclass

from .kb import KnowledgeBase, Listing

MAX_RESULTS = 3
REQUIRED = ("budget_usd", "district", "rooms")


@dataclass
class Criteria:
    budget_usd: int | None = None
    district: str | None = None
    rooms: int | None = None

    def missing(self) -> list[str]:
        return [name for name in REQUIRED if getattr(self, name) in (None, "")]


def recommend(kb: KnowledgeBase, c: Criteria) -> list[Listing]:
    """Only available listings (F-06), never above budget, best value first."""
    hits = [
        l for l in kb.available()
        if (c.budget_usd is None or l.rent_usd <= c.budget_usd)
        and (not c.district or l.district.lower() == c.district.lower())
        and (c.rooms is None or l.rooms == c.rooms)
    ]
    hits.sort(key=lambda l: (l.rent_usd / l.area_m2, l.rent_usd))
    return hits[:MAX_RESULTS]
