"""Twitch chat reader against a scripted fake WebSocket server."""

from __future__ import annotations

import asyncio

import pytest

from engine.signals.hype import is_hype
from engine.signals.twitch_chat import TwitchChat, parse_irc_line

pytestmark = pytest.mark.anyio

PRIVMSG = (
    "@badge-info=;badges=;color=#0000FF;display-name=SomeViewer;emotes=25:0-4;"
    "id=abc;mod=0;room-id=1337;tmi-sent-ts=1642696567751;user-id=42 "
    ":someviewer!someviewer@someviewer.tmi.twitch.tv PRIVMSG #Shroud :Kappa that was : insane"
)


def test_parse_privmsg_with_tags():
    tags, command, params = parse_irc_line(PRIVMSG)
    assert command == "PRIVMSG"
    assert params == ["#Shroud", "Kappa that was : insane"]  # colons in the text survive
    assert tags["emotes"] == "25:0-4" and tags["tmi-sent-ts"] == "1642696567751"


@pytest.mark.parametrize(
    ("line", "command", "params"),
    [
        ("PING :tmi.twitch.tv", "PING", ["tmi.twitch.tv"]),
        (
            ":tmi.twitch.tv 001 justinfan123 :Welcome, GLHF!",
            "001",
            ["justinfan123", "Welcome, GLHF!"],
        ),
        (":tmi.twitch.tv RECONNECT", "RECONNECT", []),
        ("", "", []),
        ("@only=tags", "", []),
    ],
)
def test_parse_other_lines(line, command, params):
    _tags, got_command, got_params = parse_irc_line(line)
    assert (got_command, got_params) == (command, params)


@pytest.mark.parametrize(
    ("text", "emotes", "expected"),
    [
        ("KEKW", False, True),
        ("that was INSANE no way", False, True),
        ("CLIP IT", False, True),
        ("?????", False, True),
        ("LETS GOOOOO BABY", False, True),
        ("hello", True, True),  # a native emote tag counts
        ("what game is this", False, False),
        ("hi chat how are you", False, False),
        ("kekw", False, False),  # emote names are case-sensitive
        ("", False, False),
    ],
)
def test_is_hype(text, emotes, expected):
    assert is_hype(text, emotes) is expected


class FakeSocket:
    def __init__(self, script):
        self.sent: list[str] = []
        self._incoming: asyncio.Queue = asyncio.Queue()
        for frame in script:
            self._incoming.put_nowait(frame)

    async def send(self, text):
        self.sent.append(text)

    def feed(self, frame):
        self._incoming.put_nowait(frame)

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self._incoming.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConnect:
    def __init__(self, *sockets):
        self.sockets = list(sockets)
        self.opened = 0

    def __call__(self, url, **kwargs):
        self.opened += 1
        return self.sockets.pop(0)


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def test_anonymous_login_join_ping_and_messages():
    socket = FakeSocket([":tmi.twitch.tv 001 justinfan1 :Welcome, GLHF!\r\n"])
    received = []
    chat = TwitchChat(received.append, connect=FakeConnect(socket))
    chat.set_channels({"Shroud", "pokimane"})
    task = asyncio.create_task(chat.run())
    await settle()
    assert chat.connected
    assert socket.sent[0] == "CAP REQ :twitch.tv/tags twitch.tv/commands"
    assert socket.sent[1].startswith("NICK justinfan") and "PASS" not in " ".join(socket.sent)
    assert socket.sent[2] == "JOIN #pokimane,#shroud"

    socket.feed("PING :tmi.twitch.tv\r\n" + PRIVMSG + "\r\n")
    await settle()
    assert "PONG :tmi.twitch.tv" in socket.sent
    (message,) = received
    assert (message.channel, message.text, message.has_emotes) == (
        "shroud",
        "Kappa that was : insane",
        True,
    )
    assert not hasattr(message, "user") and "someviewer" not in repr(message)

    chat.set_channels({"shroud", "xqc"})  # one leaves, one joins
    await settle()
    assert socket.sent[-2:] == ["PART #pokimane", "JOIN #xqc"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_reconnects_when_asked_and_rejoins(monkeypatch):
    monkeypatch.setattr("engine.signals.twitch_chat.random.random", lambda: 0.0)
    real_sleep = asyncio.sleep
    monkeypatch.setattr("engine.signals.twitch_chat.asyncio.sleep", lambda _s: real_sleep(0))
    first = FakeSocket([":tmi.twitch.tv 001 x :Welcome\r\n", ":tmi.twitch.tv RECONNECT\r\n"])
    second = FakeSocket([":tmi.twitch.tv 001 x :Welcome\r\n"])
    connect = FakeConnect(first, second)
    chat = TwitchChat(lambda m: None, connect=connect)
    chat.set_channels({"shroud"})
    task = asyncio.create_task(chat.run())
    for _ in range(10):
        await settle()
    assert connect.opened == 2 and chat.connected
    assert "JOIN #shroud" in second.sent
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_idle_until_there_is_a_channel_and_handler_errors_are_contained():
    socket = FakeSocket([":tmi.twitch.tv 001 x :Welcome\r\n", PRIVMSG + "\r\n", PRIVMSG + "\r\n"])
    connect = FakeConnect(socket)
    seen = []

    def handler(message):
        seen.append(message)
        raise RuntimeError("boom")

    chat = TwitchChat(handler, connect=connect)
    task = asyncio.create_task(chat.run())
    await settle()
    assert connect.opened == 0  # nothing to read: no connection at all
    chat.set_channels({"shroud"})
    await settle()
    assert connect.opened == 1 and len(seen) == 2  # a failing handler did not kill the reader
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
