"""Speech-to-text with word timestamps. Only the clip window is ever sent.

Default provider: Deepgram's pre-recorded API (POST /v1/listen). Any provider that
returns words with start and end times can be added behind the same interface.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx

from engine.render.captions import Word

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


class TranscriptionError(Exception):
    pass


class TranscriptionKeyMissing(TranscriptionError):
    pass


class Transcriber(Protocol):
    def transcribe(self, audio: Path) -> list[Word]: ...


class DeepgramTranscriber:
    def __init__(
        self,
        api_key: Callable[[], str | None],
        language: str = "multi",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._language = language
        self._transport = transport

    def transcribe(self, audio: Path) -> list[Word]:
        key = self._api_key()
        if not key:
            raise TranscriptionKeyMissing("no transcription API key is set")
        params = {"model": "nova-3", "smart_format": "true", "language": self._language}
        headers = {"Authorization": f"Token {key}", "Content-Type": "audio/flac"}
        try:
            with httpx.Client(timeout=60.0, transport=self._transport) as client:
                response = client.post(
                    DEEPGRAM_URL, params=params, headers=headers, content=audio.read_bytes()
                )
        except (httpx.HTTPError, OSError) as exc:
            raise TranscriptionError(f"transcription failed ({type(exc).__name__})") from None
        if response.status_code in (401, 403):
            raise TranscriptionError("the transcription service rejected the API key")
        if response.status_code != 200:
            raise TranscriptionError(f"the transcription service returned {response.status_code}")
        try:
            raw = response.json()["results"]["channels"][0]["alternatives"][0].get("words", [])
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise TranscriptionError("the transcription service sent an unreadable reply") from None
        words = []
        for item in raw if isinstance(raw, list) else []:
            try:
                text = str(item.get("punctuated_word") or item["word"])
                start, end = float(item["start"]), float(item["end"])
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if text.strip() and end >= start >= 0:
                words.append(Word(text=text.strip()[:80], start=start, end=end))
        return words
