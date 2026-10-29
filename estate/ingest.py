"""Turn a realtor's listing post into a Listing (F-01).

Realtors write posts by hand, in Uzbek or Russian, so the parser looks for each
field independently and leaves anything it cannot find empty instead of guessing.
"""
from __future__ import annotations

import re

from .kb import Listing

DISTRICTS = {
    "Chilonzor": ("chilonzor", "чиланзар", "чилонзор"),
    "Yunusobod": ("yunusobod", "юнусабад", "юнусобод"),
    "Mirzo Ulug'bek": ("mirzo ulug'bek", "mirzo ulugbek", "мирзо улугбек", "мирзо-улугбек"),
    "Shayxontohur": ("shayxontohur", "шайхантахур", "шайхонтоҳур"),
    "Yakkasaroy": ("yakkasaroy", "яккасарай", "яккасарой"),
    "Mirobod": ("mirobod", "мирабад", "миробод"),
    "Sergeli": ("sergeli", "сергели"),
    "Olmazor": ("olmazor", "алмазар", "олмазор"),
    "Uchtepa": ("uchtepa", "учтепа"),
    "Yashnobod": ("yashnobod", "яшнабад", "яшнобод"),
    "Bektemir": ("bektemir", "бектемир"),
    "Yangihayot": ("yangihayot", "янгиҳаёт", "янгихаят"),
}
AMENITIES = {
    "parking": ("parkovka", "парковк", "parking", "avtoturargoh"),
    "furnished": ("mebel", "мебел", "jihozlangan", "furnished"),
    "air conditioning": ("konditsioner", "кондиционер", "konditsaner"),
    "washing machine": ("kir yuvish", "стиральн"),
    "elevator": ("lift", "лифт"),
    "wifi": ("wifi", "wi-fi", "internet", "интернет"),
    "balcony": ("balkon", "балкон"),
}

# One optional space as a thousands separator, never a newline: "2 400$" is 2400,
# but "Qavat: 4\n450$" must stay 450.
_PRICE = re.compile(r"(\d{1,3}(?: \d{3})?|\d{4,6}) ?(?:\$|usd|у\.е|dollar|доллар)|\$ ?(\d{1,3}(?: \d{3})?|\d{4,6})", re.I)
# "mebelsiz" / "без мебели" mean the amenity is absent.
_NEGATED = re.compile(r"siz\b")
_ROOMS = re.compile(r"(\d)\s*[-\s]?(?:xonali|xona|хонали|хона|комн|room|-к\b)", re.I)
_AREA = re.compile(r"(\d{2,3}(?:[.,]\d)?)\s*(?:m2|m²|м2|м²|kv\.?\s*m|кв\.?\s*м)", re.I)


def _num(s: str) -> int:
    return int(re.sub(r"\D", "", s))


def _present(low: str, key: str) -> bool:
    """True if the keyword occurs at least once without a negation ("-siz" after, "без" before)."""
    for m in re.finditer(re.escape(key), low):
        rest = re.match(r"\w*", low[m.end():]).group(0)
        if rest.endswith("siz") or low[max(0, m.start() - 4):m.start()].strip().endswith("без"):
            continue
        return True
    return False


def _district(low: str) -> str:
    """The district mentioned earliest in the post, not the first one in the table."""
    hits = [(low.find(k), name) for name, keys in DISTRICTS.items() for k in keys if k in low]
    return min(hits)[1] if hits else ""


def parse_post(listing_id: str, text: str, post_url: str = "", photos: list[str] | None = None) -> Listing:
    low = text.lower()
    price = _PRICE.search(text)
    rooms = _ROOMS.search(text)
    area = _AREA.search(text)
    district = _district(low)
    amenities = [name for name, keys in AMENITIES.items() if any(_present(low, k) for k in keys)]
    title = text.strip().splitlines()[0][:80] if text.strip() else listing_id
    return Listing(
        id=listing_id,
        title=title,
        district=district,
        rooms=int(rooms.group(1)) if rooms else 0,
        area_m2=float(area.group(1).replace(",", ".")) if area else 0.0,
        rent_usd=_num(price.group(1) or price.group(2)) if price else 0,
        amenities=amenities,
        photos=photos or [],
        post_url=post_url,
    )


def is_complete(listing: Listing) -> bool:
    """Hold posts missing mandatory card fields (including area) for correction."""
    return listing.complete
