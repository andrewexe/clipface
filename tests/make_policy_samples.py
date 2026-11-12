"""Make sample clips for checking the policy gate with a real Anthropic key.

    uv run python -m tests.make_policy_samples out_folder
    uv run python -m engine.policy.check out_folder/address_on_screen.mp4

Expected: `clean` finds nothing; `music` reports music; `address_on_screen` and
`private_message_on_screen` are reported by the content check. All content is generated:
the address and the names are made up.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests import audio_samples


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "policy_samples")
    out.mkdir(parents=True, exist_ok=True)
    quiet = audio_samples.noise(seconds=10) * 0.2
    audio_samples.clip_with_audio(out / "clean.mp4", quiet)
    audio_samples.clip_with_audio(out / "music.mp4", audio_samples.song(seconds=10))
    audio_samples.clip_with_audio(
        out / "address_on_screen.mp4", quiet, text="Ship to 4821 Maple Grove Lane Springfield"
    )
    audio_samples.clip_with_audio(
        out / "private_message_on_screen.mp4", quiet, text="DM from Alex - dont tell anyone ok"
    )
    for path in sorted(out.glob("*.mp4")):
        print(path)


if __name__ == "__main__":
    main()
