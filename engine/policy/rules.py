"""Clipping-program rules and the credit every post must carry.

A streamer's `program_rules` (set when their permission status is "program") can hold:
  required_tags: hashtags that must be in the description
  credit_format: e.g. "Clip from {name} - watch live: {url}"
  banned: words or topics the program does not allow
"""

from __future__ import annotations

import re

DEFAULT_CREDIT = "Clip from {name}: {url}"
_TAG = re.compile(r"^#?[\w]{1,50}$")


def normalise_rules(raw: dict | None) -> dict:
    """Clean, bounded copy of stored or submitted rules."""
    raw = raw if isinstance(raw, dict) else {}

    def strings(key: str, limit: int, each: int) -> list[str]:
        values = raw.get(key)
        if not isinstance(values, list):
            return []
        cleaned = [" ".join(str(v).split())[:each] for v in values if isinstance(v, str | int)]
        return [v for v in dict.fromkeys(cleaned) if v][:limit]

    tags = ["#" + t.lstrip("#") for t in strings("required_tags", 100, 51) if _TAG.match(t)][:15]
    credit = raw.get("credit_format")
    credit = " ".join(credit.split())[:200] if isinstance(credit, str) else ""
    return {
        "required_tags": tags,
        "credit_format": credit,
        "banned": strings("banned", 50, 60),
    }


def credit_line(name: str, url: str, rules: dict | None = None) -> str:
    """Always names the streamer and links their channel, whatever the format says."""
    template = normalise_rules(rules)["credit_format"] or DEFAULT_CREDIT
    try:
        line = template.format(name=name, url=url)
    except (KeyError, IndexError, ValueError):
        line = DEFAULT_CREDIT.format(name=name, url=url)
    if url not in line:
        line = f"{line} {url}"
    if name not in line:
        line = f"{line} ({name})"
    return line


def build_description(caption: str | None, name: str, url: str, rules: dict | None = None) -> str:
    """Caption, then any required tags not already there, then the credit line."""
    clean = normalise_rules(rules)
    parts = [caption.strip()] if caption and caption.strip() else []
    present = {tag.lower() for tag in re.findall(r"#\w+", caption or "")}
    missing = [tag for tag in clean["required_tags"] if tag.lower() not in present]
    if missing:
        parts.append(" ".join(missing))
    parts.append(credit_line(name, url, clean))
    return "\n\n".join(parts)
