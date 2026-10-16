"""Answer buyer questions strictly from listing data. Unknown -> say so, never guess."""
from __future__ import annotations

from .listings import Listing

UNKNOWN = "I don't have that in the listing yet; I'll check with the owner and come back to you."


def answer(listing: Listing, question: str) -> str:
    q = question.lower()
    if "parking" in q:
        return "Yes, parking is included." if listing.parking else "No dedicated parking."
    if "furnish" in q:
        return "It comes furnished." if listing.furnished else "It is sold unfurnished."
    if "price" in q or "cost" in q or "how much" in q:
        return f"The asking price is ${listing.price_usd:,}."
    if "metro" in q:
        return f"About {listing.metro_minutes} minutes' walk to the metro."
    if "floor" in q:
        return f"Floor {listing.floor} of {listing.floors_total}."
    if "size" in q or "area" in q or "m2" in q or "square" in q:
        return f"{listing.area_m2:g} m2, {listing.rooms} room(s)."
    for f in listing.features:
        if any(word in q for word in f.lower().split()):
            return f"Yes: {f}."
    return UNKNOWN
