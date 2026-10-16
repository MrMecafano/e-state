"""Independent checker: verify a reply before it is sent (F-10, F-06, F-11, F-12).

It does not trust the composer. A reply is blocked if it names a listing that is
not in the KB or is taken, leaks an owner's name or phone, or asks for money.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .kb import KnowledgeBase

_ID = re.compile(r"\bL-\d+\b")
_PHONE = re.compile(r"\+?998[\s-]?\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}")
_ASKS_MONEY = re.compile(r"(send|transfer|pay|o'tkaz|to'la|тўла|переведите|оплатите)\s.{0,30}(prepay|deposit|oldindan|zaklad|предоплат|залог|\$\d+)", re.I)


@dataclass
class Verdict:
    ok: bool
    reasons: list[str]


def check(reply: str, kb: KnowledgeBase, realtor_contact: str = "") -> Verdict:
    reasons: list[str] = []
    for lid in set(_ID.findall(reply)):
        listing = kb.get(lid)
        if listing is None:
            reasons.append(f"{lid} is not in the knowledge base")
        elif not listing.available:
            reasons.append(f"{lid} is taken")
    for l in kb.all():
        if l.owner_phone and l.owner_phone in reply.replace(" ", ""):
            reasons.append(f"owner phone of {l.id} leaked")
        if l.owner_name and re.search(rf"\b{re.escape(l.owner_name)}\b", reply):
            reasons.append(f"owner name of {l.id} leaked")
    for phone in _PHONE.findall(reply):
        if realtor_contact and phone.replace(" ", "") in realtor_contact.replace(" ", ""):
            continue
        if not any(phone.replace(" ", "") == l.owner_phone for l in kb.all()):
            reasons.append(f"unknown phone number {phone}")
    if _ASKS_MONEY.search(reply):
        reasons.append("reply asks the client for money")
    return Verdict(ok=not reasons, reasons=sorted(set(reasons)))
