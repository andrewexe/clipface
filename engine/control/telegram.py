"""Phone approvals through a Telegram bot (official Bot API, long polling).

What it does: sends each clip that needs a person to your phone, with Approve and Reject
buttons; forwards alerts (a strike, an expired sign-in); and takes /pause, /resume and
/status.

Who it obeys: exactly one private chat, the one that sent `/start <code>` with the
pairing code shown in the app. Messages and button presses from anywhere else are
ignored. The bot can only do what is listed above: it cannot read keys, run commands,
change settings, or un-reject a clip.

The bot token is part of every request URL, so URLs are never logged or put in errors.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets as pysecrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from engine.settings import SettingsStore

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
POLL_SECONDS = 25
MAX_VIDEO_BYTES = 49 * 1024 * 1024  # Telegram's limit for bots is 50 MB
CAPTION_MAX = 1000
_CALLBACK = re.compile(r"^(a|r):(\d{1,12})$")

HELP = (
    "Clips that need your review arrive here with Approve and Reject buttons.\n"
    "/pause stops all publishing now\n"
    "/resume turns publishing back on\n"
    "/status shows the queue and whether publishing is paused"
)


@dataclass(frozen=True)
class Card:
    caption: str
    video: Path | None


class BotActions(Protocol):
    def card(self, clip_id: int) -> Card | None: ...
    def approve(self, clip_id: int) -> str: ...
    def reject(self, clip_id: int) -> str: ...
    def set_paused(self, paused: bool) -> str: ...
    def status(self) -> str: ...
    def paired(self, chat_id: int) -> None: ...


class _BadToken(Exception):
    pass


class _ApiError(Exception):
    pass


def keyboard(clip_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"a:{clip_id}"},
                {"text": "❌ Reject", "callback_data": f"r:{clip_id}"},
            ]
        ]
    }


class TelegramBot:
    def __init__(
        self,
        token: Callable[[], str | None],
        settings: SettingsStore,
        actions: BotActions,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._settings = settings
        self._actions = actions
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(POLL_SECONDS + 15, connect=10), transport=transport
        )
        self._offset = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[tuple[str, Any]] | None = None
        self.pairing_code = self._new_code()
        self.state = "no_token"  # no_token | bad_token | waiting_to_pair | ready | error
        self.bot_username: str | None = None
        self._username_for: str | None = None

    @staticmethod
    def _new_code() -> str:
        return pysecrets.token_hex(4).upper()

    # -- called from other threads ----------------------------------------------------

    def notify_clip(self, clip_id: int) -> None:
        self._enqueue(("clip", clip_id))

    def alert(self, text: str) -> None:
        self._enqueue(("alert", text))

    def _enqueue(self, item: tuple[str, Any]) -> None:
        if self._loop is not None and self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)

    def wake(self) -> None:
        pass

    # -- service ----------------------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        sender = asyncio.create_task(self._send_loop())
        try:
            while True:
                token = await asyncio.to_thread(self._token)
                if not token:
                    self.state = "no_token"
                    await asyncio.sleep(10)
                    continue
                try:
                    await self.poll(token)
                except _BadToken:
                    self.state = "bad_token"
                    await asyncio.sleep(30)
                except (httpx.HTTPError, _ApiError) as exc:
                    self.state = "error"
                    log.info("telegram poll failed: %s", type(exc).__name__)
                    await asyncio.sleep(5)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, token: str, method: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.post(f"{API}/bot{token}/{method}", **kwargs)
        except httpx.HTTPError as exc:
            # Re-raise without the request: its URL contains the token.
            raise httpx.HTTPError(type(exc).__name__) from None
        if response.status_code in (401, 404):
            raise _BadToken
        try:
            body = response.json()
        except ValueError:
            raise _ApiError(f"{method}: unreadable reply") from None
        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description") if isinstance(body, dict) else None
            raise _ApiError(f"{method}: {description or response.status_code}")
        return body.get("result")

    @property
    def chat_id(self) -> int:
        return self._settings.get().telegram_chat_id

    async def poll(self, token: str) -> None:
        """One long-poll round: fetch updates and handle each."""
        if self._username_for != token:
            me = await self._call(token, "getMe")
            self.bot_username = me.get("username") if isinstance(me, dict) else None
            self._username_for = token
        self.state = "ready" if self.chat_id else "waiting_to_pair"
        updates = await self._call(
            token,
            "getUpdates",
            json={
                "offset": self._offset,
                "timeout": POLL_SECONDS,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        for update in updates if isinstance(updates, list) else []:
            if not isinstance(update, dict):
                continue
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            try:
                if isinstance(update.get("callback_query"), dict):
                    await self._on_button(token, update["callback_query"])
                elif isinstance(update.get("message"), dict):
                    await self._on_message(token, update["message"])
            except (_ApiError, httpx.HTTPError) as exc:
                log.info("telegram reply failed: %s", type(exc).__name__)
            except Exception:
                log.exception("telegram update failed")

    # -- incoming -----------------------------------------------------------------------

    async def _say(self, token: str, chat_id: int, text: str) -> None:
        await self._call(token, "sendMessage", json={"chat_id": chat_id, "text": text[:4000]})

    async def _on_message(self, token: str, message: dict) -> None:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id, text = chat.get("id"), str(message.get("text") or "").strip()
        if chat.get("type") != "private" or not isinstance(chat_id, int):
            return  # never in groups or channels
        if text.startswith("/start"):
            code = text.removeprefix("/start").strip().upper()
            if code and hmac.compare_digest(code.encode(), self.pairing_code.encode()):
                await asyncio.to_thread(self._actions.paired, chat_id)
                self.pairing_code = self._new_code()  # single use
                self.state = "ready"
                await self._say(token, chat_id, "This phone is now paired with Clipper.\n\n" + HELP)
            return  # a wrong or missing code gets no reply at all
        if chat_id != self.chat_id:
            return
        command = text.split()[0].split("@")[0].lower() if text else ""
        if command == "/pause":
            reply = await asyncio.to_thread(self._actions.set_paused, True)
        elif command == "/resume":
            reply = await asyncio.to_thread(self._actions.set_paused, False)
        elif command == "/status":
            reply = await asyncio.to_thread(self._actions.status)
        else:
            reply = HELP
        await self._say(token, chat_id, reply)

    async def _on_button(self, token: str, query: dict) -> None:
        message = query.get("message") if isinstance(query.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        query_id = query.get("id")
        match = _CALLBACK.match(str(query.get("data") or ""))
        if not self.chat_id or chat.get("id") != self.chat_id or chat.get("type") != "private":
            await self._call(
                token,
                "answerCallbackQuery",
                json={"callback_query_id": query_id, "text": "Not allowed."},
            )
            return
        if not match:
            await self._call(token, "answerCallbackQuery", json={"callback_query_id": query_id})
            return
        action, clip_id = match.group(1), int(match.group(2))
        do = self._actions.approve if action == "a" else self._actions.reject
        result = await asyncio.to_thread(do, clip_id)
        await self._call(
            token, "answerCallbackQuery", json={"callback_query_id": query_id, "text": result[:190]}
        )
        # Replace the buttons with the outcome, so the card can't be acted on twice.
        base = {"chat_id": self.chat_id, "message_id": message.get("message_id")}
        if "caption" in message:
            text = f"{message.get('caption') or ''}\n\n{result}"[:1024]
            await self._call(token, "editMessageCaption", json={**base, "caption": text})
        else:
            text = f"{message.get('text') or ''}\n\n{result}"[:4000]
            await self._call(token, "editMessageText", json={**base, "text": text})

    # -- outgoing -----------------------------------------------------------------------

    async def _send_loop(self) -> None:
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            try:
                await self.send(item)
            except _BadToken:
                self.state = "bad_token"
            except (httpx.HTTPError, _ApiError) as exc:
                log.info("telegram send failed: %s", type(exc).__name__)
            except Exception:
                log.exception("telegram send crashed")

    async def send(self, item: tuple[str, Any]) -> bool:
        """Deliver one queued item. False when there is nobody to send it to."""
        settings = self._settings.get()
        token = await asyncio.to_thread(self._token)
        if not token or not settings.telegram_chat_id:
            return False
        chat_id = settings.telegram_chat_id
        kind, payload = item
        if kind == "alert":
            await self._say(token, chat_id, f"⚠️ {payload}")
            return True
        if not settings.telegram_notify:
            return False
        card = await asyncio.to_thread(self._actions.card, payload)
        if card is None:
            return False
        markup = json.dumps(keyboard(payload))
        caption = card.caption[:CAPTION_MAX]
        video = card.video
        if video is not None and video.is_file() and video.stat().st_size <= MAX_VIDEO_BYTES:
            data = await asyncio.to_thread(video.read_bytes)
            await self._call(
                token,
                "sendVideo",
                data={
                    "chat_id": str(chat_id),
                    "caption": caption,
                    "reply_markup": markup,
                    "supports_streaming": "true",
                },
                files={"video": (video.name, data, "video/mp4")},
            )
        else:
            await self._call(
                token,
                "sendMessage",
                json={"chat_id": chat_id, "text": caption, "reply_markup": keyboard(payload)},
            )
        return True
