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

_PRICE = re.compile(r"(\d[\d\s]{1,6})\s*(?:\$|usd|у\.е|dollar|доллар)|\$\s*(\d[\d\s]{1,6})", re.I)
_ROOMS = re.compile(r"(\d)\s*[-\s]?(?:xonali|xona|хонали|хона|комн|room|-к\b)", re.I)
_AREA = re.compile(r"(\d{2,3}(?:[.,]\d)?)\s*(?:m2|m²|м2|м²|kv\.?\s*m|кв\.?\s*м)", re.I)


def _num(s: str) -> int:
    return int(re.sub(r"\D", "", s))


def parse_post(listing_id: str, text: str, post_url: str = "", photos: list[str] | None = None) -> Listing:
    low = text.lower()
    price = _PRICE.search(text)
    rooms = _ROOMS.search(text)
    area = _AREA.search(text)
    district = next((name for name, keys in DISTRICTS.items() if any(k in low for k in keys)), "")
    amenities = [name for name, keys in AMENITIES.items() if any(k in low for k in keys)]
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
    """A listing missing price, district or rooms is held for the realtor to fix, not offered."""
    return bool(listing.rent_usd and listing.district and listing.rooms)
