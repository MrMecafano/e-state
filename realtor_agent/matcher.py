"""Rank listings for a buyer. Hard filters first, then a soft score."""
from __future__ import annotations

from dataclasses import dataclass, field

from .listings import Listing


@dataclass
class Criteria:
    budget_usd: int | None = None
    district: str | None = None
    rooms: int | None = None
    must_have: list[str] = field(default_factory=list)  # e.g. ["parking", "elevator"]
    max_metro_minutes: int | None = None


def _has(listing: Listing, need: str) -> bool:
    need = need.lower()
    if need == "parking":
        return listing.parking
    if need == "furnished":
        return listing.furnished
    return any(need in f.lower() for f in listing.features)


def passes(listing: Listing, c: Criteria) -> bool:
    if c.budget_usd is not None and listing.price_usd > c.budget_usd:
        return False
    if c.district and listing.district.lower() != c.district.lower():
        return False
    if c.rooms is not None and listing.rooms != c.rooms:
        return False
    if c.max_metro_minutes is not None and listing.metro_minutes > c.max_metro_minutes:
        return False
    return all(_has(listing, n) for n in c.must_have)


def score(listing: Listing, c: Criteria) -> float:
    """Higher is better. Cheaper relative to budget, closer to metro, more space per dollar."""
    s = 0.0
    if c.budget_usd:
        s += 50 * (1 - listing.price_usd / c.budget_usd)
    s += max(0, 20 - listing.metro_minutes)
    s += 1000 * listing.area_m2 / listing.price_usd
    return round(s, 2)


def match(listings: list[Listing], c: Criteria, limit: int = 5) -> list[tuple[Listing, float]]:
    hits = [(l, score(l, c)) for l in listings if passes(l, c)]
    hits.sort(key=lambda t: (-t[1], t[0].price_usd))
    return hits[:limit]
