"""Record live Twitch chat activity to a JSONL file for detector tuning.

    uv run python -m tests.record_chat --minutes 25 --out chat.jsonl channel1 channel2 ...

Privacy: message text and user names are NOT saved. Each line is only
{"t": seconds since start, "ch": channel, "n": text length, "hype": 0/1}.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path

from engine.signals.hype import is_hype
from engine.signals.twitch_chat import ChatMessage, TwitchChat


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=25)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("channels", nargs="+")
    args = parser.parse_args()

    started = time.time()
    counts: Counter[str] = Counter()
    with args.out.open("w") as out:

        def on_message(message: ChatMessage) -> None:
            counts[message.channel] += 1
            row = {
                "t": round(message.ts - started, 3),
                "ch": message.channel,
                "n": len(message.text),
                "hype": int(is_hype(message.text, message.has_emotes)),
            }
            out.write(json.dumps(row) + "\n")

        chat = TwitchChat(on_message)
        chat.set_channels(set(args.channels))
        task = asyncio.create_task(chat.run())
        try:
            while time.time() - started < args.minutes * 60:
                await asyncio.sleep(30)
                out.flush()
                print(
                    f"{(time.time() - started) / 60:5.1f} min  connected={chat.connected}  "
                    f"messages={sum(counts.values())}  active={dict(counts.most_common(6))}",
                    flush=True,
                )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    print("done", dict(counts.most_common()))


if __name__ == "__main__":
    asyncio.run(main())
