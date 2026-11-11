"""Read-only Twitch chat over the official IRC WebSocket endpoint.

Connects without an account, using the long-standing anonymous `justinfan` login.
Note: Twitch's current docs describe only token logins (a user access token with
chat:read), so the anonymous login is tolerated rather than documented. If it ever
stops working, `connected` stays False and the detector falls back to its other signals.

Only what the detector needs is kept from each message: channel, time, and text.
User names are never read or stored.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

import websockets

log = logging.getLogger(__name__)

URL = "wss://irc-ws.chat.twitch.tv:443"
_JOINS_PER_BATCH = 15  # Twitch allows 20 join attempts per 10 seconds
_JOIN_BATCH_PAUSE_S = 11.0


@dataclass(frozen=True)
class ChatMessage:
    channel: str
    ts: float  # arrival time, unix seconds
    text: str
    has_emotes: bool


def parse_irc_line(line: str) -> tuple[dict[str, str], str, list[str]]:
    """Split one IRC line into (tags, command, params). The trailing param keeps spaces."""
    tags: dict[str, str] = {}
    rest = line
    if rest.startswith("@"):
        raw_tags, _, rest = rest[1:].partition(" ")
        for pair in raw_tags.split(";"):
            key, _, value = pair.partition("=")
            tags[key] = value
    if rest.startswith(":"):
        _prefix, _, rest = rest[1:].partition(" ")
    head, sep, trailing = rest.partition(" :")
    parts = head.split()
    if not parts:
        return tags, "", []
    params = parts[1:] + ([trailing] if sep else [])
    return tags, parts[0].upper(), params


class TwitchChat:
    def __init__(
        self,
        on_message: Callable[[ChatMessage], None],
        connect: Callable = websockets.connect,
        url: str = URL,
    ) -> None:
        self._on_message = on_message
        self._connect = connect
        self._url = url
        self._wanted: set[str] = set()
        self._joined: set[str] = set()
        self._changed: asyncio.Event | None = None
        self.connected = False
        self.last_message_at: float | None = None

    def set_channels(self, channels: set[str]) -> None:
        """Channels to read. Call from the event loop that runs `run()`."""
        channels = {name.lower() for name in channels}
        if channels != self._wanted:
            self._wanted = channels
            if self._changed is not None:
                self._changed.set()

    async def run(self) -> None:
        self._changed = asyncio.Event()
        backoff = 1.0
        while True:
            if not self._wanted:
                self._changed.clear()
                await self._changed.wait()
                continue
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - network errors of every kind
                log.info("twitch chat disconnected: %s", type(exc).__name__)
            finally:
                self.connected = False
                self._joined = set()
            backoff = 1.0 if time.monotonic() - started > 60 else min(60.0, backoff * 2)
            await asyncio.sleep(backoff + random.random())

    async def _session(self) -> None:
        async with self._connect(self._url, open_timeout=15, ping_interval=None) as ws:
            await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await ws.send(f"NICK justinfan{random.randint(10_000, 99_999)}")
            syncer = asyncio.create_task(self._sync_channels(ws))
            try:
                async for frame in ws:
                    if isinstance(frame, bytes):
                        frame = frame.decode("utf-8", errors="replace")
                    for line in frame.split("\r\n"):
                        if line and await self._handle(ws, line):
                            return  # server asked us to reconnect
                    if not self._wanted:
                        return
            finally:
                syncer.cancel()
                await asyncio.gather(syncer, return_exceptions=True)

    async def _handle(self, ws, line: str) -> bool:
        tags, command, params = parse_irc_line(line)
        if command == "PING":
            await ws.send("PONG :" + (params[-1] if params else "tmi.twitch.tv"))
        elif command == "001":
            self.connected = True
            log.info("twitch chat connected")
        elif command == "PRIVMSG" and len(params) >= 2:
            self.last_message_at = time.time()
            try:
                self._on_message(
                    ChatMessage(
                        channel=params[0].lstrip("#").lower(),
                        ts=self.last_message_at,
                        text=params[1],
                        has_emotes=bool(tags.get("emotes")),
                    )
                )
            except Exception:
                log.exception("chat handler failed")
        elif command == "RECONNECT":
            return True
        elif command == "NOTICE" and params and "Login authentication failed" in params[-1]:
            log.warning("twitch chat refused the anonymous login")
            return True
        return False

    async def _sync_channels(self, ws) -> None:
        """Keep the joined set equal to the wanted set, inside Twitch's join rate limit."""
        assert self._changed is not None
        while True:
            self._changed.clear()
            leave = sorted(self._joined - self._wanted)
            if leave:
                await ws.send("PART " + ",".join("#" + name for name in leave))
                self._joined -= set(leave)
            join = sorted(self._wanted - self._joined)
            while join:
                batch, join = join[:_JOINS_PER_BATCH], join[_JOINS_PER_BATCH:]
                await ws.send("JOIN " + ",".join("#" + name for name in batch))
                self._joined |= set(batch)
                if join:
                    await asyncio.sleep(_JOIN_BATCH_PAUSE_S)
            await self._changed.wait()
