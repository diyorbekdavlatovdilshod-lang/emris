"""Lightweight language detection for incoming messages.

The result is only a *hint* passed to Claude ("their latest message is in
..."); the model still decides the final reply language. We only distinguish
Uzbek ("uz"), Russian ("ru") and English ("en"), or return ``None`` when the
text carries no letters at all (pure emoji / numbers / punctuation).

The heuristic is deliberately simple and dependency-free so it runs fast on
every message and is easy to unit-test offline.
"""

from __future__ import annotations

import re
from typing import Optional

# Every apostrophe-like glyph Uzbek Latin uses for o'/g' (oʻ/gʻ).
APOSTROPHES = "'ʻ’‘`"  # ' ʻ ’ ‘ `

# Uzbek words written in Latin script. Matched as whole tokens so short words
# like "men"/"sen"/"siz" do not fire inside unrelated English words.
_UZ_LATIN_WORDS = {
    "salom",
    "assalom",
    "assalomu",
    "rahmat",
    "rahmatla",
    "yaxshi",
    "nima",
    "nimala",
    "qalesan",
    "qalaysan",
    "qalaysiz",
    "qanaqa",
    "qayerda",
    "qayerdasan",
    "bugun",
    "ertaga",
    "kecha",
    "kerak",
    "xop",
    "xo",  # "xo'p" after apostrophe stripping
    "bopti",
    "zor",
    "hozir",
    "keyin",
    "men",
    "sen",
    "siz",
    "biz",
    "ha",
    "yoq",
    "yaxshimisan",
    "borman",
    "bormisan",
    "uyda",
    "ishdaman",
    "tushundim",
    "mayli",
    "albatta",
}

# Uzbek words written in Cyrillic script (substring match is fine here).
_UZ_CYRILLIC_WORDS = (
    "салом",
    "ассалом",
    "рахмат",
    "рақмат",
    "яхши",
    "нима",
    "қалесан",
    "қалайсан",
    "йўқ",
    "бугун",
    "эртага",
    "керак",
    "хоп",
    "хўп",
    "зўр",
    "ҳозир",
    "кейин",
    "майли",
    "албатта",
)

# Characteristic Uzbek Cyrillic letters that Russian does not use.
_UZ_CYRILLIC_LETTERS = "ўқғҳ"  # ў қ ғ ҳ

# Uzbek verb/question suffixes that strongly mark a Latin word as Uzbek.
_UZ_SUFFIXES = (
    "misan",
    "misiz",
    "mizmi",
    "yapsan",
    "yapman",
    "yapti",
    "yapmiz",
    "asizmi",
    "ganman",
    "dingmi",
    "yapsiz",
    "ngmi",
)

# English contractions / possessives that must NOT be read as Uzbek oʻ/gʻ.
_EN_AFTER_APOSTROPHE = ("clock", "s", "t", "re", "ve", "ll", "d", "m")

_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_TOKEN_RE = re.compile(r"[A-Za-z]+")


def _has_uzbek_apostrophe(text: str) -> bool:
    """True if the text contains a genuine Uzbek oʻ/gʻ, not an English one."""
    for match in re.finditer(r"[ogOG]([" + re.escape(APOSTROPHES) + r"])", text):
        tail = text[match.end() : match.end() + 6].lower()
        # English possessive/contraction right after the apostrophe (at a word
        # boundary) -> not Uzbek. e.g. "dog's", "o'clock", "it's".
        is_english = False
        for suffix in _EN_AFTER_APOSTROPHE:
            if tail.startswith(suffix):
                rest = tail[len(suffix) :]
                if not rest or not rest[0].isalpha():
                    is_english = True
                    break
        if not is_english:
            return True
    return False


def _looks_uzbek_latin(text: str) -> bool:
    if _has_uzbek_apostrophe(text):
        return True
    # Strip apostrophes so "o'zbek" tokenizes with its letters intact.
    stripped = text
    for ch in APOSTROPHES:
        stripped = stripped.replace(ch, "")
    tokens = [t.lower() for t in _TOKEN_RE.findall(stripped)]
    for token in tokens:
        if token in _UZ_LATIN_WORDS:
            return True
        if any(token.endswith(suffix) for suffix in _UZ_SUFFIXES):
            return True
    return False


def _looks_uzbek_cyrillic(text: str) -> bool:
    lowered = text.lower()
    if any(ch in lowered for ch in _UZ_CYRILLIC_LETTERS):
        return True
    return any(word in lowered for word in _UZ_CYRILLIC_WORDS)


def detect_language(text: str) -> Optional[str]:
    """Return "uz", "ru", "en" or ``None`` (no letters) for ``text``."""
    if not text:
        return None

    cyrillic = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))

    if cyrillic == 0 and latin == 0:
        return None  # emoji, digits, punctuation only -> no hint

    if cyrillic > latin:
        return "uz" if _looks_uzbek_cyrillic(text) else "ru"

    # Latin-dominant (ties fall here too).
    return "uz" if _looks_uzbek_latin(text) else "en"
