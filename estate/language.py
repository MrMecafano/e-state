"""Detect the client's language so we answer in it (F-08)."""
from __future__ import annotations

import re
import unicodedata

# Uzbek keyboards use several visually equivalent apostrophes, including the
# modifier-letter apostrophes that are not punctuation to Python's regex engine.
_APOSTROPHES = str.maketrans({char: "'" for char in "‘’ʻʼ`´"})
_UZ_CYRL = re.compile(r"[ўқғҳ]", re.I)
_CYRL = re.compile(r"[а-яё]", re.I)
_UZ_CYRL_WORDS = re.compile(r"(керак|хонали|\bменга\b|\bбор\b|\bборми\b|\bуй\b|\bижара|\bнарх|ассалому)", re.I)
_UZ_LATN_HINTS = re.compile(
    r"(o'|g'|\bkerak\b|\bxona\w*|\bijara\w*|\bnarx|\bqancha\b|\bbormi\b|\bassalomu\b|"
    r"\bpul\w*|\bkorish\b|\bko'|\bto'|\bbo'|\bolaman\b)", re.I)


def normalize_text(text: str) -> str:
    """Normalize spelling/format, not meaning; shared by input routing and checks."""
    # Remove zero-width format characters so they cannot hide a contact token.
    text = "".join(" " if unicodedata.category(c) == "Zs" else c
                   for c in text if unicodedata.category(c) != "Cf")
    return unicodedata.normalize("NFKC", text.translate(_APOSTROPHES)).casefold()


def detect(text: str) -> str:
    """Return 'uz-cyrl', 'ru', 'uz-latn' or 'en'."""
    text = normalize_text(text)
    if _UZ_CYRL.search(text) or _UZ_CYRL_WORDS.search(text):
        return "uz-cyrl"
    if _CYRL.search(text):
        return "ru"
    if _UZ_LATN_HINTS.search(text):
        return "uz-latn"
    return "en"
