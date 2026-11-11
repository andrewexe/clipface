"""Is a chat message a "hype" message? Used for the emote/keyword burst signal."""

from __future__ import annotations

import re

# Emote names are case-sensitive words in chat (native, BTTV, FFZ and 7TV emotes all
# arrive as plain text).
HYPE_EMOTES = frozenset(
    """
    LUL LULW KEKW KEKL OMEGALUL ICANT PepeLaugh xdd
    Pog PogChamp PogU POGGERS POGGIES POGCRAZY LETSGO EZ Clap GIGACHAD BASED W
    monkaS monkaW WutFace NotLikeThis D: OMG HOLY Kreygasm Cinema Aware
    """.split()
)

_HYPE_WORDS = re.compile(
    r"\b(clip\s+(it|that)|clipped?|no\s+way|holy|wtf|omg|lmf?ao+|lo+l|insane|"
    r"let'?s\s+go+|what\s+was\s+that|w+o+w+)\b",
    re.IGNORECASE,
)
_PUNCT_BURST = re.compile(r"[!?]{3,}")


def is_hype(text: str, has_emotes: bool = False) -> bool:
    if has_emotes or _HYPE_WORDS.search(text) or _PUNCT_BURST.search(text):
        return True
    words = text.split()
    if any(word in HYPE_EMOTES for word in words):
        return True
    # SHOUTING: mostly capitals in a message with some length.
    letters = [ch for ch in text if ch.isalpha()]
    return len(letters) >= 6 and sum(ch.isupper() for ch in letters) / len(letters) > 0.8
