"""Detect the client's language so we answer in it (F-08).

Clients write Uzbek (Latin or Cyrillic) or Russian, so replies use only those.
A message with no clear language ("700$", "ok", English) keeps the language of
the client's earlier messages; with none yet, the reply uses both ("both").
"""
from __future__ import annotations

import re
import unicodedata

# Uzbek keyboards use several visually equivalent apostrophes, including the
# modifier-letter apostrophes that are not punctuation to Python's regex engine.
_APOSTROPHES = str.maketrans({char: "'" for char in "‘’ʻʼ`´"})
_UZ_CYRL = re.compile(r"[ўқғҳ]", re.I)
_CYRL = re.compile(r"[а-яё]", re.I)
_UZ_CYRL_WORDS = re.compile(r"(керак|хонали|\bменга\b|\bбор\b|\bборми\b|\bуй\b|\bижара|\bнарх|ассалому|"
                            r"\bнечта\b|\bнеча\b|\bбугун\b|\bучун\b|\bбилан\b|\bэмас\b|\bмижоз|\bмумкин)", re.I)
_UZ_LATN_HINTS = re.compile(
    r"(o'|g'|\bkerak\b|\bxona\w*|\bijara\w*|\bnarx|\bqancha\b|\bbormi\b|\bassalomu\b|\bsalom\b|"
    r"\bpul\w*|\bkorish\b|\bko'|\bto'|\bbo'|\bolaman\b|\bgacha\b|\btuman\w*|\buy\w*|\brahmat\b|"
    r"\bkvartira\w*|\brieltor\w*|\bmenga\b|\bbor\b|\byaxshi\b|\bqayer|\bnechta\b|\bnecha\b|\bbugun\b|"
    r"\bhozir\b|\buchun\b|\bbilan\b|\bemas\b|\byana\b|\bqaysi\b|\bmijoz\w*|\bjoy\w*|\bmumkin\w*|"
    r"\bqil\w*|\byoz\w*|\bnima\b|\bqanday\b|\bqachon\b|\bkecha\b|\bertaga\b|\bberildi\b|\bsiz\w*)", re.I)


def normalize_text(text: str) -> str:
    """Normalize spelling/format, not meaning; shared by input routing and checks."""
    # Remove zero-width format characters so they cannot hide a contact token.
    text = "".join(" " if unicodedata.category(c) == "Zs" else c
                   for c in text if unicodedata.category(c) != "Cf")
    return unicodedata.normalize("NFKC", text.translate(_APOSTROPHES)).casefold()


def clear_language(text: str) -> str | None:
    """'uz-cyrl', 'ru' or 'uz-latn' when the text shows its language, else None."""
    text = normalize_text(text)
    if _UZ_CYRL.search(text) or _UZ_CYRL_WORDS.search(text):
        return "uz-cyrl"
    if _CYRL.search(text):
        return "ru"
    if _UZ_LATN_HINTS.search(text):
        return "uz-latn"
    return None


def detect(text: str) -> str:
    """Return 'uz-cyrl', 'ru' or 'uz-latn' (the default when the text is unclear)."""
    return clear_language(text) or "uz-latn"


def conversation_language(message: str, history: list[dict]) -> str:
    """This message's language, else the client's latest clear one, else "both"."""
    for text in [message] + [h["text"] for h in reversed(history) if h.get("role") == "user"]:
        lang = clear_language(text)
        if lang:
            return lang
    return "both"
