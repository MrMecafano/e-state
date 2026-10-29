"""Independent send gate: listing validity, private contacts and payment requests.

The composer and all listing text/URLs are untrusted. A configured realtor
contact is an exact allowlist entry, never an exception to owner privacy.
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote, unquote_plus, urlsplit

from .kb import KnowledgeBase
from .language import normalize_text

_ID = re.compile(r"\bL-\d+\b", re.I)
# Brackets are the card/reference marker, including custom and unknown KB IDs.
_BRACKET_ID = re.compile(r"\[([^\[\]\r\n]+)\]")
# Read the entire phone-shaped token, not a whitelisted substring of it.
# Separators include parentheses, hyphens, NBSP and line-wrapped contacts.
_PHONE = re.compile(r"(?<![\w+])\+?\d(?:[\d\s().\-–—]*\d)?(?!\d)")
_MONEY = re.compile(
    r"\b(?:money|deposit\w*|prepay\w*|payment\w*|cash|rent|advance|funds|"
    r"pul\w*|depozit\w*|zaklad\w*|avans\w*|oldindan|to'lov\w*|"
    r"тўлов\w*|пул\w*|олдиндан|предоплат\w*|залог\w*|депозит\w*|аванс\w*|"
    r"оплат\w*|деньг\w*|аренд\w*|бронь)\b|\bpay\s+(?:now|first|us|me|in advance)\b|[$€₽]\s*\d|"
    r"\b\d[\d ,.]*(?:\s*(?:usd|dollars?|доллар\w*|so'm|сум|руб\w*)\b|\s*\$)")
_ACTION = re.compile(
    r"\b(?:send|transfer|pay|wire|remit|prepay|make|provide|"
    r"yubor(?:ing|ishingiz|ish)?|o'tkaz(?:ing|ishingiz|ish)?|to'la(?:ng|shingiz|sh|b)?|"
    r"tashlang|qiling|oshiring|юборинг|ўтказинг|тўланг|қилинг|оширинг|"
    r"отправ(?:ьте|ь|ить)|пришл(?:ите|и)|перевед(?:ите|и)|перевести|"
    r"заплат(?:ите|и|ить)|оплат(?:ите|и|ить)|внес(?:ите|и|ти))\b")
_REQUIRED = re.compile(r"\b(?:required|mandatory|must be paid|kerak|shart|talab qilinadi|"
                       r"керак|шарт|талаб қилинади|обязатель\w*|требуется|необходимо|нужно)\b")
_NEGATED_ACTION = re.compile(
    r"(?:\bdo not|\bdon't|\bnever|\bnot to|\bне|\bno need to|\bnot required to|"
    r"\b(?:do not|don't) (?:need|have) to|"
    r"\b(?:never|do not|don't) (?:ask|require)(?:\s+\w+){0,4} to)\s*$")
# 'Pay' is itself a monetary instruction; a second money noun is not required.
# Exclude ordinary non-monetary idioms rather than banning all uses of the word.
_PAY = re.compile(r"\b(?:pay|prepay)\b(?!\s+(?:attention|respect|tribute|a visit)\b)")
_NEGATED_REQUIRED = re.compile(r"(?:\b(?:no|not|never|without|не|никакой|без)|\bisn't|\baren't)\s*$")
_CLAUSES = re.compile(r"(?<!\d)\.|\.(?!\d)|[!?;]|\b(?:but|however|но|однако|lekin|ammo|лекин|аммо)\b")


@dataclass
class Verdict:
    ok: bool
    reasons: list[str]


def _visible_text(text: str, *, form_encoded: bool = False) -> str:
    # Check URL query/path contents and HTML entities as well as visible prose.
    # Preserve both normal '+' phone prefixes and form-encoded '+' separators.
    # Two rounds also cover a percent-encoded contact behind an encoded redirect.
    decode = unquote_plus if form_encoded else unquote
    for _ in range(2):
        text = html.unescape(decode(text))
    return normalize_text(text)


def _phone_key(token: str) -> str:
    digits = "".join(str(unicodedata.decimal(c)) for c in token if c.isdecimal())
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 9:  # Uzbekistan local form, e.g. (90) 123-45-67
        digits = "998" + digits
    return digits


def _phones(text: str) -> set[str]:
    result = set()
    for match in _PHONE.finditer(text):
        token = match.group()
        digits = sum(c.isdecimal() for c in token)
        # Ordinary prices, dates, room/area figures and short post IDs aren't phones.
        if digits >= 9 or (token.startswith("+") and digits >= 7):
            result.add(_phone_key(token))
    return result


def _asks_money(text: str) -> bool:
    """Block requests/requirements, not rent figures or explicit non-payment advice."""
    for clause in _CLAUSES.split(text):
        for action in _PAY.finditer(clause):
            if not _NEGATED_ACTION.search(clause[:action.start()]):
                return True
        money = list(_MONEY.finditer(clause))
        if not money:
            continue
        for action in _ACTION.finditer(clause):
            if _NEGATED_ACTION.search(clause[:action.start()]):
                continue
            if any(abs(action.start() - amount.start()) <= 100 for amount in money):
                return True
        for required in _REQUIRED.finditer(clause):
            for amount in money:
                if abs(required.start() - amount.start()) > 100:
                    continue
                # Only negation attached to this noun/requirement counts; an
                # unrelated "no kitchen" must not exempt "deposit required".
                negated = (_NEGATED_REQUIRED.search(clause[:amount.start()])
                           or _NEGATED_REQUIRED.search(clause[:required.start()])
                           or re.match(r"\s+(?:emas|эмас)\b", clause[required.end():]))
                if not negated:
                    return True
    return False


def _credential_url(text: str) -> bool:
    """Inspect URLs in all outgoing prose, not just dedicated media/link fields."""
    for url in re.findall(r"https?://[^\s<>\"']+", text, re.I):
        try:
            parsed = urlsplit(url)
            if ((parsed.hostname or "").rstrip(".").lower() == "api.telegram.org"
                    or parsed.username is not None or parsed.password is not None
                    or re.search(r"[?&](?:access_token|api_key|bot_token)=|/bot[0-9]+:", url, re.I)):
                return True
        except ValueError:
            return True  # Malformed URLs must not bypass the credential gate.
    return False


def check(reply: str, kb: KnowledgeBase, realtor_contact: str = "") -> Verdict:
    reasons: list[str] = []
    texts = {_visible_text(reply), _visible_text(reply, form_encoded=True)}
    listings = kb.all()
    ids = {lid.strip().upper() for text in texts
           for lid in _ID.findall(text) + _BRACKET_ID.findall(text)}
    for lid in ids:
        listing = kb.get(lid)
        if listing is None:
            reasons.append(f"{lid} is not in the knowledge base")
        elif not listing.available:
            reasons.append(f"{lid} is taken")
        elif not listing.complete:
            reasons.append(f"{lid} has incomplete mandatory fields")
    if any(_credential_url(text) for text in texts):
        reasons.append("reply contains a credential-bearing URL")
    # Only real, available, complete listing tokens are structural IDs rather
    # than contacts. Keep all surrounding prose/URLs and ordinary numbers intact.
    valid_ids = {listing.id.upper() for listing in listings if listing.available and listing.complete}
    phone_texts = {_ID.sub(lambda match: " " if match.group().upper() in valid_ids else match.group(), text)
                   for text in texts}
    phones = {phone for text in phone_texts for phone in _phones(text)}
    allowed = _phones(_visible_text(realtor_contact))
    owners = set()
    for listing in listings:
        owner_phones = _phones(_visible_text(listing.owner_phone))
        owners.update(owner_phones)
        if phones & owner_phones:
            reasons.append(f"owner phone of {listing.id} leaked")
        name = _visible_text(listing.owner_name).strip()
        if name:
            pattern = r"\s+".join(re.escape(part) for part in name.split())
            if any(re.search(rf"(?<!\w){pattern}(?!\w)", text) for text in texts):
                reasons.append(f"owner name of {listing.id} leaked")
    for phone in phones - owners - allowed:
        reasons.append(f"unknown phone number {phone}")
    if any(_asks_money(text) for text in texts):
        reasons.append("reply asks the client for money")
    return Verdict(ok=not reasons, reasons=sorted(set(reasons)))
