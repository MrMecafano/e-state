"""Pull budget, district and rooms out of a client's free-text message (F-03).

Anything not stated stays None, so the agent asks for it instead of assuming.
"""
from __future__ import annotations

import re

from .ingest import DISTRICTS
from .matcher import Criteria

# One optional space as a thousands separator, never a newline: a number on the
# line above the budget must not merge into it (see ingest.py's _PRICE).
_BUDGET = re.compile(
    r"(\d{1,3}(?: \d{3})?|\d{4,6}) ?(?:\$|usd|у\.е|dollar|доллар)|\$ ?(\d{1,3}(?: \d{3})?|\d{4,6})", re.I)
_ROOMS_NUM = re.compile(r"(\d)\s*[-\s]?(?:xonali|xona|хонали|хона|комн|room)", re.I)
_ROOM_WORDS = {
    1: ("bir xonali", "бир хонали", "однокомн", "одно комн", "studio", "студи"),
    2: ("ikki xonali", "икки хонали", "двухкомн", "двух комн"),
    3: ("uch xonali", "уч хонали", "трехкомн", "трёхкомн", "трех комн"),
    4: ("to'rt xonali", "тўрт хонали", "четырехкомн", "четырёхкомн"),
}


def extract(text: str) -> Criteria:
    low = text.lower()
    budget = _BUDGET.search(text)
    rooms_m = _ROOMS_NUM.search(text)
    rooms = int(rooms_m.group(1)) if rooms_m else next(
        (n for n, words in _ROOM_WORDS.items() if any(w in low for w in words)), None)
    district = next((name for name, keys in DISTRICTS.items() if any(k in low for k in keys)), None)
    return Criteria(
        budget_usd=int(re.sub(r"\D", "", budget.group(1) or budget.group(2))) if budget else None,
        district=district,
        rooms=rooms,
    )
