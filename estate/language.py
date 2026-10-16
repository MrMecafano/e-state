"""Detect the client's language so we answer in it (F-08)."""
from __future__ import annotations

import re

_UZ_CYRL = re.compile(r"[ўқғҳЎҚҒҲ]")
_CYRL = re.compile(r"[а-яёА-ЯЁ]")
_UZ_CYRL_WORDS = re.compile(r"(керак|хонали|\bменга\b|\bбор\b|\bборми\b|\bуй\b|\bижара|\bнарх|ассалому)", re.I)
_UZ_LATN_HINTS = re.compile(r"(o'|g'|o‘|g‘|\bkerak\b|\bxona\b|\bijara\b|\bnarx|\bqancha\b|\bbormi\b|\bassalomu\b)", re.I)


def detect(text: str) -> str:
    """Return 'uz-cyrl', 'ru', 'uz-latn' or 'en'."""
    if _UZ_CYRL.search(text) or _UZ_CYRL_WORDS.search(text):
        return "uz-cyrl"
    if _CYRL.search(text):
        return "ru"
    if _UZ_LATN_HINTS.search(text):
        return "uz-latn"
    return "en"
