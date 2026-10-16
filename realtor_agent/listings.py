"""Listing model and loading."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "listings.json"


@dataclass(frozen=True)
class Listing:
    id: str
    title: str
    district: str
    rooms: int
    area_m2: float
    price_usd: int
    floor: int
    floors_total: int
    parking: bool
    furnished: bool
    metro_minutes: int
    features: list[str] = field(default_factory=list)


def load_listings(path: Path | str = DEFAULT_PATH) -> list[Listing]:
    with open(path, encoding="utf-8") as f:
        return [Listing(**row) for row in json.load(f)]


def by_id(listings: list[Listing], listing_id: str) -> Listing | None:
    return next((l for l in listings if l.id == listing_id), None)
