"""Per-realtor knowledge base of listings with availability (F-01, F-05, F-06, N-02)."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

DEMO_DATA = Path(__file__).resolve().parent.parent / "data" / "listings.json"


@dataclass(frozen=True)
class Listing:
    id: str
    title: str
    district: str
    rooms: int
    area_m2: float
    rent_usd: int  # per month
    amenities: list[str] = field(default_factory=list)
    photos: list[str] = field(default_factory=list)
    post_url: str = ""
    owner_name: str = ""   # private: never shown to clients (F-12)
    owner_phone: str = ""  # private: never shown to clients (F-12)
    status: str = "available"  # "available" | "taken"
    videos: list[str] = field(default_factory=list)  # appended for positional compatibility

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", self.id.strip().upper())

    @property
    def complete(self) -> bool:
        """Incomplete posts stay in the KB for correction, never in recommendations."""
        return (isinstance(self.title, str) and bool(self.title.strip())
                and isinstance(self.district, str) and bool(self.district.strip())
                and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) and value > 0
                        for value in (self.rooms, self.area_m2, self.rent_usd)))

    @property
    def available(self) -> bool:
        return self.status == "available"


# Control-bot commands (F-05). Uzbek / Russian / English words for "taken" and "free".
_TAKEN = r"(taken|band|bandi|олинди|занят[ао]?|сдан[ао]?)"
_FREE = r"(available|free|bo'sh|бўш|свободн[ао])"
_CMD = re.compile(rf"^\s*(?:(?P<id1>L-\d+)\s+(?P<s1>{_TAKEN}|{_FREE})|(?P<s2>{_TAKEN}|{_FREE})\s+(?P<id2>L-\d+))\s*$", re.I)


class KnowledgeBase:
    """One instance per realtor. Instances never share data (N-02)."""

    def __init__(self, realtor_id: str, listings: list[Listing] | None = None) -> None:
        self.realtor_id = realtor_id
        self._listings: dict[str, Listing] = {l.id: l for l in (listings or [])}

    @classmethod
    def from_json(cls, realtor_id: str, path: Path | str = DEMO_DATA) -> "KnowledgeBase":
        with open(path, encoding="utf-8") as f:
            return cls(realtor_id, [Listing(**row) for row in json.load(f)])

    def add(self, listing: Listing) -> None:
        """Ingest a listing parsed from the realtor's post (F-01)."""
        self._listings[listing.id] = listing

    def get(self, listing_id: str) -> Listing | None:
        return self._listings.get(listing_id.strip().upper())

    def all(self) -> list[Listing]:
        return list(self._listings.values())

    def available(self) -> list[Listing]:
        """Only these may ever be offered to a client (F-06)."""
        return [l for l in self._listings.values() if l.available and l.complete]

    def apply_control_message(self, text: str) -> str:
        """Handle a one-line control-bot message like "L-003 taken" or "bo'sh L-003" (F-05)."""
        m = _CMD.match(text)
        if not m:
            return "Send: <listing id> taken | available, e.g. L-003 taken"
        lid = (m.group("id1") or m.group("id2")).upper()
        word = (m.group("s1") or m.group("s2")).lower()
        listing = self._listings.get(lid)
        if listing is None:
            return f"No listing {lid} in your knowledge base."
        status = "taken" if re.fullmatch(_TAKEN, word, re.I) else "available"
        self._listings[lid] = replace(listing, status=status)
        return f"{lid} marked {status}."
