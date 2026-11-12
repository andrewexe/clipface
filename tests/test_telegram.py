"""Phone approvals through a fake Telegram. Includes the Phase 9 check: approving from
the phone publishes within the scheduler window."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from engine.control.bot_actions import EngineBotActions
from engine.control.telegram import TelegramBot
from engine.db.models import Account, Clip, Moment, PublishJob, Streamer, StreamSession
from engine.publish.service import Publisher
from engine.publish.youtube import YouTubeClient, YouTubePublisher
from tests.fake_youtube import CLIENT, REFRESH, FakeYouTube

pytestmark = pytest.mark.anyio

TOKEN = "123456:TELEGRAM-TOKEN-DO-NOT-LEAK"
OWNER, STRANGER = 111, 999
T0 = datetime(2026, 11, 19, 14, 0, tzinfo=UTC)


class FakeTelegram:
    def __init__(self):
        self.updates: list[dict] = []
        self.sent: list[tuple[str, dict]] = []
        self.next_update = 1
        self.fail_with: int | None = None

    def push(self, **update):
        self.updates.append({"update_id": self.next_update, **update})
        self.next_update += 1

    def message(self, chat_id, text, chat_type="private"):
        self.push(
            message={"message_id": 1, "chat": {"id": chat_id, "type": chat_type}, "text": text}
        )

    def button(self, chat_id, data, caption="A clip", chat_type="private"):
        self.push(
            callback_query={
                "id": f"q{self.next_update}",
                "data": data,
                "message": {
                    "message_id": 77,
                    "chat": {"id": chat_id, "type": chat_type},
                    "caption": caption,
                },
            }
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        prefix = f"/bot{TOKEN}/"
        if not request.url.path.startswith(prefix):
            return httpx.Response(
                401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
            )
        method = request.url.path.removeprefix(prefix)
        if self.fail_with:
            return httpx.Response(
                self.fail_with, json={"ok": False, "description": "Too Many Requests"}
            )
        if method == "getMe":
            return httpx.Response(
                200, json={"ok": True, "result": {"username": "clipper_test_bot"}}
            )
        if method == "getUpdates":
            body = json.loads(request.content)
            assert body["allowed_updates"] == ["message", "callback_query"]
            ready = [u for u in self.updates if u["update_id"] >= body["offset"]]
            self.updates = []
            return httpx.Response(200, json={"ok": True, "result": ready})
        if request.headers["content-type"].startswith("multipart/"):
            payload = {"_multipart": request.content}
        else:
            payload = json.loads(request.content)
        self.sent.append((method, payload))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})

    def said(self, method="sendMessage"):
        return [payload.get("text") for name, payload in self.sent if name == method]


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def phone(harness):
    ctx = harness.ctx
    ctx.config.clips_dir.mkdir(parents=True, exist_ok=True)
    video = ctx.config.clips_dir / "clip_1.mp4"
    video.write_bytes(b"\x00\x01" * 2048)
    ctx.settings.update({"export_dir": str(ctx.config.data_dir / "export")})
    ctx.secrets.set("acct_youtube_00000000000000aa", REFRESH)
    with ctx.session_factory.begin() as db:
        db.add(
            Streamer(
                platform="twitch",
                username="shroud",
                display_name="shroud",
                permission_status="granted",
            )
        )
        db.add(
            Account(
                platform="youtube", handle="My Clips", keychain_ref="acct_youtube_00000000000000aa"
            )
        )
        db.flush()
        db.add(StreamSession(streamer_id=1, started_at=T0))
        db.flush()
        for title in ("He actually did it", "Second clip"):
            moment = Moment(session_id=1, peak_ts=T0, status="clipped")
            db.add(moment)
            db.flush()
            db.add(Clip(moment_id=moment.id, start_ts=T0, end_ts=T0 + timedelta(seconds=34), title=title,
                        status="pending", file_path=str(video), claude_score=0.71, meta={"duration": 34.0},
                        policy_result={"verdict": "flag", "findings": [
                            {"check": "music", "verdict": "flag", "detail": "music was detected in the audio"}]}))  # fmt: skip
    clock = Clock()
    youtube = FakeYouTube(now=clock)
    client = YouTubeClient(lambda: CLIENT, transport=httpx.MockTransport(youtube))
    ctx.publisher = Publisher(ctx.session_factory, ctx.settings, ctx.secrets,
                              {"youtube": YouTubePublisher(client, clock)}, now=clock,
                              uniform=lambda low, high: (low + high) / 2, tz=UTC)  # fmt: skip
    telegram = FakeTelegram()
    bot = TelegramBot(
        lambda: TOKEN, ctx.settings, EngineBotActions(ctx), transport=httpx.MockTransport(telegram)
    )
    ctx.telegram = bot
    harness.bot, harness.telegram, harness.youtube, harness.clock = bot, telegram, youtube, clock
    yield harness
    client.close()


def clip(phone, clip_id=1):
    with phone.ctx.session_factory() as db:
        return db.get(Clip, clip_id)


async def pair(phone):
    phone.telegram.message(OWNER, f"/start {phone.bot.pairing_code}")
    await phone.bot.poll(TOKEN)
    assert phone.ctx.settings.get().telegram_chat_id == OWNER
    phone.telegram.sent.clear()


# -- pairing ----------------------------------------------------------------------------


async def test_pairing_needs_the_code_shown_in_the_app(phone):
    code = phone.bot.pairing_code
    phone.telegram.message(STRANGER, "/start")
    phone.telegram.message(STRANGER, "/start WRONGCODE")
    phone.telegram.message(STRANGER, f"/start {code}", chat_type="group")  # never in a group
    phone.telegram.message(STRANGER, "/pause")
    await phone.bot.poll(TOKEN)
    assert phone.ctx.settings.get().telegram_chat_id == 0
    assert phone.telegram.sent == []  # strangers get no reply at all
    assert phone.bot.state == "waiting_to_pair" and phone.bot.bot_username == "clipper_test_bot"

    phone.telegram.message(OWNER, f"/start {code.lower()}")
    await phone.bot.poll(TOKEN)
    assert phone.ctx.settings.get().telegram_chat_id == OWNER and phone.bot.state == "ready"
    assert "paired" in phone.telegram.said()[0]
    assert phone.bot.pairing_code != code  # the code works once

    phone.telegram.message(STRANGER, f"/start {code}")  # the old code, replayed
    await phone.bot.poll(TOKEN)
    assert phone.ctx.settings.get().telegram_chat_id == OWNER


async def test_pairing_api_shows_the_code_until_paired_and_can_unpair(phone):
    body = phone.client.get("/control/telegram", headers=phone.auth).json()
    assert body["paired"] is False and body["pairing_code"] == phone.bot.pairing_code
    assert phone.client.post("/control/telegram/test", headers=phone.auth).status_code == 409
    await pair(phone)
    body = phone.client.get("/control/telegram", headers=phone.auth).json()
    assert body["paired"] is True and body["pairing_code"] is None
    assert (
        phone.client.post("/control/telegram/unpair", headers=phone.auth).json()["paired"] is False
    )
    assert phone.ctx.settings.get().telegram_chat_id == 0
    assert phone.client.get("/control/telegram").status_code == 401


# -- clips to the phone -------------------------------------------------------------------


async def test_a_clip_needing_review_arrives_with_buttons(phone):
    assert await phone.bot.send(("clip", 1)) is False  # nobody paired: nothing is sent
    await pair(phone)
    assert await phone.bot.send(("clip", 1)) is True
    (method, payload) = phone.telegram.sent[0]
    assert method == "sendVideo"
    body = payload["_multipart"]
    assert b'name="video"; filename="clip_1.mp4"' in body and str(OWNER).encode() in body
    assert b"He actually did it" in body and b"score 0.71" in body
    assert b"music was detected in the audio" in body
    assert b'"callback_data": "a:1"' in body and b'"callback_data": "r:1"' in body


async def test_big_or_missing_video_falls_back_to_text_and_handled_clips_are_not_sent(phone):
    await pair(phone)
    with phone.ctx.session_factory.begin() as db:
        db.get(Clip, 1).file_path = str(phone.ctx.config.clips_dir / "gone.mp4")
        db.get(Clip, 2).status = "approved"
    assert await phone.bot.send(("clip", 1)) is True
    (method, payload) = phone.telegram.sent[0]
    assert (
        method == "sendMessage"
        and payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "a:1"
    )
    assert await phone.bot.send(("clip", 2)) is False  # already approved: no card
    assert await phone.bot.send(("clip", 999)) is False
    phone.ctx.settings.update({"telegram_notify": False})
    assert await phone.bot.send(("clip", 1)) is False


# -- the Phase 9 check ------------------------------------------------------------------------


async def test_approving_from_the_phone_publishes_within_the_scheduler_window(phone):
    await pair(phone)
    phone.telegram.button(OWNER, "a:1", caption="He actually did it")
    await phone.bot.poll(TOKEN)

    assert clip(phone).status == "approved"
    with phone.ctx.session_factory() as db:
        job = db.scalars(select(PublishJob)).one()
    # Scheduled a few minutes out, inside the posting window and the day's cap.
    assert T0 + timedelta(minutes=1) <= job.scheduled_at <= T0 + timedelta(minutes=10)
    assert 9 <= job.scheduled_at.hour < 23

    answers = [p for m, p in phone.telegram.sent if m == "answerCallbackQuery"]
    assert answers[0]["text"] == "✅ Approved. 1 post(s) scheduled."
    edits = [p for m, p in phone.telegram.sent if m == "editMessageCaption"]
    assert edits[0]["caption"].endswith("✅ Approved. 1 post(s) scheduled.")
    assert "reply_markup" not in edits[0]  # the buttons are gone

    phone.ctx.publisher.run_due()
    assert phone.youtube.upload_count() == 0  # not before its time
    phone.clock.now = job.scheduled_at + timedelta(seconds=30)
    phone.ctx.publisher.run_due()
    assert phone.youtube.upload_count() == 1
    assert clip(phone).status == "published"
    audit = phone.client.get("/audit", headers=phone.auth).json()["entries"]
    approve = next(e for e in audit if e["action"] == "clip.approve")
    assert approve["actor"] == "user" and approve["details"]["via"] == "telegram"


async def test_reject_and_pressing_twice(phone):
    await pair(phone)
    phone.telegram.button(OWNER, "r:1")
    phone.telegram.button(OWNER, "a:1")  # a second press on the same card
    phone.telegram.button(OWNER, "a:999")
    await phone.bot.poll(TOKEN)
    assert clip(phone).status == "rejected"
    answers = [p["text"] for m, p in phone.telegram.sent if m == "answerCallbackQuery"]
    assert answers == [
        "❌ Rejected.",
        "That clip was already handled.",
        "That clip no longer exists.",
    ]
    with phone.ctx.session_factory() as db:
        assert db.scalars(select(PublishJob)).all() == []


@pytest.mark.parametrize(
    ("chat_id", "chat_type", "data"),
    [
        (STRANGER, "private", "a:1"),  # someone else found the bot
        (OWNER, "group", "a:1"),  # the owner, but in a group where others can press
        (OWNER, "private", "a:1; drop table clips"),
        (OWNER, "private", "x:1"),
        (OWNER, "private", "a:-1"),
        (OWNER, "private", ""),
    ],
)
async def test_buttons_from_anyone_else_or_malformed_do_nothing(phone, chat_id, chat_type, data):
    await pair(phone)
    phone.telegram.button(chat_id, data, chat_type=chat_type)
    await phone.bot.poll(TOKEN)
    assert clip(phone).status == "pending"
    assert all(m == "answerCallbackQuery" for m, _p in phone.telegram.sent)  # no edits, no messages


async def test_buttons_do_nothing_before_pairing(phone):
    phone.telegram.button(OWNER, "a:1")
    await phone.bot.poll(TOKEN)
    assert clip(phone).status == "pending"


# -- commands and alerts --------------------------------------------------------------------


async def test_pause_resume_and_status_from_the_phone(phone):
    await pair(phone)
    phone.telegram.message(OWNER, "/pause")
    await phone.bot.poll(TOKEN)
    assert phone.ctx.settings.get().publishing_paused is True
    phone.telegram.message(OWNER, "/status")
    phone.telegram.message(OWNER, "/resume@clipper_test_bot")
    phone.telegram.message(OWNER, "hello?")
    await phone.bot.poll(TOKEN)
    assert phone.ctx.settings.get().publishing_paused is False
    said = phone.telegram.said()
    assert said[0] == "⏸ All publishing is paused."
    assert "Publishing: PAUSED" in said[1] and "Clips waiting for review: 2" in said[1]
    assert said[2] == "▶️ Publishing is back on." and "/pause" in said[3]
    audit = [e for e in phone.client.get("/audit", headers=phone.auth).json()["entries"]]
    pauses = [e for e in audit if e["action"] == "publishing.pause"]
    assert pauses[0]["details"] == {"via": "telegram"}


async def test_paused_from_the_phone_stops_a_scheduled_post(phone):
    await pair(phone)
    phone.telegram.button(OWNER, "a:1")
    phone.telegram.message(OWNER, "/pause")
    await phone.bot.poll(TOKEN)
    phone.clock.now = T0 + timedelta(hours=1)
    phone.ctx.publisher.run_due()
    assert phone.youtube.upload_count() == 0


async def test_alerts_reach_the_phone(phone):
    await pair(phone)
    phone.ctx.publisher.on_alert(lambda text: asyncio.get_event_loop_policy)  # another listener
    assert await phone.bot.send(("alert", "My Clips is paused: claim.")) is True
    assert phone.telegram.said() == ["⚠️ My Clips is paused: claim."]


async def test_running_service_delivers_notifications_from_other_threads(phone):
    await pair(phone)
    task = asyncio.create_task(phone.bot.run())
    await asyncio.sleep(0.2)
    threading.Thread(target=lambda: phone.bot.notify_clip(1)).start()
    threading.Thread(target=lambda: phone.bot.alert("strike!")).start()
    for _ in range(50):
        await asyncio.sleep(0.05)
        if len(phone.telegram.sent) >= 2:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sorted(m for m, _p in phone.telegram.sent) == ["sendMessage", "sendVideo"]


# -- failures and secrecy ---------------------------------------------------------------------


async def test_bad_token_and_api_errors_do_not_crash_or_leak_the_token(phone):
    phone.ctx.secrets.set("telegram_bot_token", TOKEN)  # as when the user saved it
    wrong = TelegramBot(lambda: "000:wrong", phone.ctx.settings, EngineBotActions(phone.ctx),
                        transport=httpx.MockTransport(phone.telegram))  # fmt: skip
    task = asyncio.create_task(wrong.run())
    for _ in range(40):
        await asyncio.sleep(0.02)
        if wrong.state == "bad_token":
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert wrong.state == "bad_token"

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    offline = TelegramBot(lambda: TOKEN, phone.ctx.settings, EngineBotActions(phone.ctx),
                          transport=httpx.MockTransport(boom))  # fmt: skip
    with pytest.raises(httpx.HTTPError) as excinfo:
        await offline.poll(TOKEN)
    assert TOKEN not in str(excinfo.value) and TOKEN not in repr(excinfo.value)

    await pair(phone)
    phone.telegram.fail_with = 429
    task = asyncio.create_task(phone.bot.run())
    await asyncio.sleep(0.1)
    phone.bot.notify_clip(1)
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert phone.bot.state == "error"
    assert TOKEN not in phone.log_text() and "TELEGRAM-TOKEN" not in phone.log_text()
