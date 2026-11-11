"""Burned-in captions: word timestamps in, an ASS subtitle file out.

Words are shown a few at a time, in step with speech. Transcript text is untrusted:
anything that could act as an ASS command is neutralised before it is written.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_WORDS = 3
MAX_CHARS = 18
MAX_GAP_S = 0.6
MIN_SHOW_S = 0.35
PLAY_W, PLAY_H = 1080, 1920


@dataclass(frozen=True)
class Word:
    text: str
    start: float  # seconds from the start of the clip
    end: float


@dataclass(frozen=True)
class Line:
    text: str
    start: float
    end: float


def chunk(words: list[Word]) -> list[Line]:
    groups: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        if not word.text.strip():
            continue
        if current:
            length = sum(len(w.text) for w in current) + len(current) + len(word.text)
            ends_sentence = current[-1].text.rstrip()[-1:] in ".!?"
            if (
                len(current) >= MAX_WORDS
                or length > MAX_CHARS
                or word.start - current[-1].end > MAX_GAP_S
                or ends_sentence
            ):
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)

    lines = []
    for index, group in enumerate(groups):
        start = group[0].start
        end = max(group[-1].end, start + MIN_SHOW_S)
        if index + 1 < len(groups):
            # Hold the words until the next ones arrive, but not across a long pause.
            next_start = groups[index + 1][0].start
            end = min(max(end, min(next_start, end + 0.4)), next_start)
        if end > start:
            lines.append(Line(" ".join(w.text.strip() for w in group), start, end))
    return lines


def _escape(text: str) -> str:
    cleaned = text.replace("\\", "/").replace("{", "(").replace("}", ")")
    return " ".join(cleaned.split()).upper()


def _stamp(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    hours, rest = divmod(centis, 360_000)
    minutes, rest = divmod(rest, 6_000)
    secs, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def build_ass(words: list[Word], clip_duration: float, margin_v: int = 460) -> str | None:
    """An ASS file for a 1080x1920 clip, or None if there is nothing to show."""
    lines = [
        line
        for line in chunk(words)
        if line.start < clip_duration and line.end > 0 and _escape(line.text)
    ]
    if not lines:
        return None
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_W}
PlayResY: {PLAY_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""  # noqa: E501
    events = [
        f"Dialogue: 0,{_stamp(max(0.0, line.start))},{_stamp(min(line.end, clip_duration))},"
        f"Caption,,0,0,0,,{_escape(line.text)}"
        for line in lines
    ]
    return header + "\n".join(events) + "\n"
