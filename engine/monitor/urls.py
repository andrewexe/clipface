"""Turn whatever the user pasted into (platform, username), or a clear error.

Only twitch.tv and kick.com channel URLs are accepted. Anything else is rejected rather
than guessed at: a wrong guess would silently follow the wrong channel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

MAX_INPUT_LEN = 2048

_TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv", "go.twitch.tv"}
_TWITCH_PLAYER_HOST = "player.twitch.tv"
_TWITCH_CLIPS_HOST = "clips.twitch.tv"
_KICK_HOSTS = {"kick.com", "www.kick.com", "m.kick.com"}

# Twitch logins: letters, digits, underscore; 4-25 today, a few older ones are shorter.
_TWITCH_NAME = re.compile(r"^[a-z0-9][a-z0-9_]{1,24}$")
# Kick slugs: the API accepts at most 25 characters per slug.
_KICK_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,24}$")

# First path segments that are site pages, not channels.
_TWITCH_RESERVED = {
    "directory", "videos", "video", "clip", "clips", "settings", "subscriptions", "inventory",
    "drops", "wallet", "downloads", "jobs", "p", "search", "turbo", "store", "friends",
    "following", "login", "signup", "team", "teams", "collections", "privacy", "legal",
    "broadcast", "prime", "bits", "partner", "u", "user", "event", "events", "annual-recap",
}  # fmt: skip
_KICK_RESERVED = {
    "browse", "categories", "category", "following", "search", "dashboard", "settings",
    "video", "videos", "clips", "auth", "login", "signup", "about", "terms-of-service",
    "privacy-policy", "community-guidelines", "dmca-policy", "transactions", "api",
}  # fmt: skip
# kick.com/popout/<name>/chat, twitch.tv/popout/<name>/chat, twitch.tv/moderator/<name>
_WRAPPER_SEGMENTS = {"popout", "moderator", "embed"}


class InvalidStreamerUrl(ValueError):
    """The message is written for the person who pasted the URL."""


@dataclass(frozen=True)
class StreamerRef:
    platform: str
    username: str

    @property
    def url(self) -> str:
        if self.platform == "twitch":
            return f"https://www.twitch.tv/{self.username}"
        return f"https://kick.com/{self.username}"


def parse_streamer_url(text: str) -> StreamerRef:
    raw = (text or "").strip()
    if not raw:
        raise InvalidStreamerUrl("Paste a Twitch or Kick channel URL.")
    if len(raw) > MAX_INPUT_LEN or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise InvalidStreamerUrl("That doesn't look like a channel URL.")

    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        raise InvalidStreamerUrl("That doesn't look like a channel URL.") from None
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise InvalidStreamerUrl("That doesn't look like a channel URL.")

    segments = [segment for segment in parts.path.split("/") if segment]

    if host == _TWITCH_CLIPS_HOST:
        raise InvalidStreamerUrl("That's a clip link. Paste the streamer's channel URL instead.")
    if host == _TWITCH_PLAYER_HOST:
        channel = parse_qs(parts.query).get("channel", [""])[0]
        return _build("twitch", channel, _TWITCH_NAME, set())
    if host in _TWITCH_HOSTS:
        return _from_segments("twitch", segments, _TWITCH_NAME, _TWITCH_RESERVED)
    if host in _KICK_HOSTS:
        return _from_segments("kick", segments, _KICK_NAME, _KICK_RESERVED)

    if "." not in host:
        # A bare name like "shroud": we can't tell which platform it is on.
        raise InvalidStreamerUrl(
            "Paste the full channel URL, for example https://www.twitch.tv/name "
            "or https://kick.com/name."
        )
    raise InvalidStreamerUrl("Only twitch.tv and kick.com channel URLs are supported.")


def _from_segments(
    platform: str, segments: list[str], pattern: re.Pattern[str], reserved: set[str]
) -> StreamerRef:
    if segments and segments[0].lower() in _WRAPPER_SEGMENTS:
        segments = segments[1:]
    if not segments:
        raise InvalidStreamerUrl("That URL has no channel name in it.")
    return _build(platform, segments[0], pattern, reserved)


def _build(platform: str, name: str, pattern: re.Pattern[str], reserved: set[str]) -> StreamerRef:
    username = name.lower()
    if username in reserved:
        raise InvalidStreamerUrl(
            "That link points to a video or site page, not a channel. "
            "Paste the streamer's channel URL."
        )
    if not pattern.match(username):
        raise InvalidStreamerUrl("That URL doesn't contain a valid channel name.")
    return StreamerRef(platform=platform, username=username)
