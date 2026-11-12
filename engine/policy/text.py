"""Deterministic checks on what was said: blocked words, personal details, gambling
promotion. These run locally on every clip, with or without the Claude classifier.

The built-in blocked-word list is stored as hashes (engine/policy/blocklist.txt), so the
words themselves are not in the source. Your own extra words go in the `blocked_words`
setting as plain text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
_BLOCKLIST_FILE = Path(__file__).with_name("blocklist.txt")


@dataclass(frozen=True)
class Finding:
    check: str
    verdict: str  # "flag": a person must look; "block": rejected outright
    detail: str

    def as_dict(self) -> dict:
        return {"check": self.check, "verdict": self.verdict, "detail": self.detail}


def _letters(token: str) -> str:
    return re.sub(r"[^a-z]", "", token.lower().translate(_LEET))


def _cap_runs(text: str, limit: int) -> str:
    return re.sub(rf"(.)\1{{{limit},}}", lambda m: m.group(1) * limit, text)


def token_hash(form: str) -> str:
    return hashlib.sha256(("clipper-blocklist:" + form).encode()).hexdigest()[:16]


def word_forms(token: str) -> tuple[list[str], list[str]]:
    """(normal forms, stretched forms) to look up for one token. A stretched form exists
    only when the token had a letter repeated three or more times ("nooooo")."""
    letters = _letters(token)
    if len(letters) < 3:
        return [], []
    normal = _cap_runs(letters, 2)
    forms = {normal, normal.removesuffix("s"), normal.removesuffix("z"), normal.removesuffix("es")}
    stretched: set[str] = set()
    if normal != letters:
        flat = _cap_runs(letters, 1)
        stretched = {flat, flat.removesuffix("s"), flat.removesuffix("z")}
    return sorted(f for f in forms if len(f) >= 3), sorted(f for f in stretched if len(f) >= 3)


@cache
def _built_in() -> tuple[frozenset[str], frozenset[str]]:
    normal: set[str] = set()
    stretched: set[str] = set()
    target = normal
    for line in _BLOCKLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line == "[stretched]":
            target = stretched
        elif line and not line.startswith("#") and line != "[normal]":
            target.add(line)
    return frozenset(normal), frozenset(stretched)


def blocked_word_count(text: str, extra_words: list[str] | None = None) -> int:
    """How many tokens are blocked words (built-in list or the user's own)."""
    normal, stretched = _built_in()
    extra = {_cap_runs(_letters(word), 2) for word in extra_words or []} - {""}
    hits = 0
    for token in text.split():
        forms, flat_forms = word_forms(token)
        if (
            any(token_hash(f) in normal for f in forms)
            or any(token_hash(f) in stretched for f in flat_forms)
            or any(f in extra for f in forms)
        ):
            hits += 1
    return hits


_PHONE = re.compile(r"(?<![\d.])(?:\+?\d[\s().-]{0,2}){9,14}\d(?![\d.])")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.I)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_STREET = re.compile(
    r"\b\d{1,5}\s+(?:[A-Za-z]+\s+){1,3}"
    r"(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|boulevard|blvd|court|ct|way|place|pl)\b\.?",
    re.I,
)
_SAID_PRIVATE = re.compile(
    r"\b(my|his|her|their)\s+(home\s+|real\s+)?(address|phone\s+number|social\s+security)\s+is\b"
    r"|\bi\s+live\s+at\b",
    re.I,
)

_GAMBLING_BRANDS = re.compile(
    r"\b(stake|roobet|rollbit|gamdom|duelbits|bc\.?game|csgoroll|draftkings|fanduel|"
    r"bet365|betway|1xbet|shuffle\.com|rainbet)\b",
    re.I,
)
_GAMBLING_WORDS = re.compile(
    r"\b(casino|slots?|roulette|blackjack|sportsbook|betting\s+site|free\s+spins)\b", re.I
)
_PROMO = re.compile(
    r"\b(use\s+(my\s+)?code|promo\s+code|referral\s+code|sign\s+up|link\s+in\s+(the\s+)?"
    r"(bio|description|chat)|deposit\s+(bonus|match)|free\s+spins|welcome\s+bonus)\b",
    re.I,
)


def check_text(
    text: str, extra_blocked_words: list[str] | None = None, banned: list[str] | None = None
) -> list[Finding]:
    """Findings for a transcript, title or caption. Empty means nothing was found."""
    findings: list[Finding] = []
    if not text:
        return findings

    blocked = blocked_word_count(text, extra_blocked_words)
    if blocked:
        findings.append(Finding("blocked_words", "block", f"{blocked} blocked word(s) were said"))

    personal = [
        label
        for label, pattern in (
            ("a phone or card number", _PHONE),
            ("an email address", _EMAIL),
            ("a social security number", _SSN),
            ("a street address", _STREET),
            ("someone's private details", _SAID_PRIVATE),
        )
        if pattern.search(text)
    ]
    if personal:
        findings.append(
            Finding("personal_info", "block", "the speech seems to include " + ", ".join(personal))
        )

    brand = _GAMBLING_BRANDS.search(text)
    gambling = brand or _GAMBLING_WORDS.search(text)
    if gambling and _PROMO.search(text):
        findings.append(
            Finding("gambling_promotion", "block", "gambling is promoted in the speech")
        )
    elif gambling:
        findings.append(Finding("gambling", "flag", "gambling is mentioned in the speech"))

    lowered = text.lower()
    hits = sorted({term for term in banned or [] if term and term.lower() in lowered})
    if hits:
        findings.append(
            Finding("program_rules", "flag", "banned by the clipping program: " + ", ".join(hits))
        )
    return findings
