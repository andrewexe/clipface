"""Run the policy checks on one video file and print what they find.

    python -m engine.policy.check clip.mp4 [--said "what was said in the clip"]

Uses your Anthropic key from the OS keychain for the content check (a few cents per
clip). Useful for seeing what the gate makes of a real clip before trusting autonomy.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from engine.config import load_config
from engine.control.spend import SpendLedger
from engine.db.session import create_db_engine, make_session_factory, run_migrations
from engine.keychain import KeychainUnavailable, SecretStore
from engine.logging_setup import SecretRedactor
from engine.media.probe import probe
from engine.policy.classifier import PolicyClassifier
from engine.policy.music import detect_music
from engine.policy.text import check_text
from engine.settings import SettingsStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("video", type=Path)
    parser.add_argument("--said", default="", help="transcript of the clip, if you have one")
    args = parser.parse_args()
    if not args.video.is_file():
        raise SystemExit(f"no such file: {args.video}")

    config = load_config()
    config.ensure_dirs()
    settings = SettingsStore(config.settings_file)
    db_engine = create_db_engine(config.db_path)
    run_migrations(db_engine)
    secrets = SecretStore(SecretRedactor())

    def key() -> str | None:
        try:
            return secrets.get("anthropic_api_key")
        except KeychainUnavailable:
            return None

    findings = check_text(args.said, settings.get().blocked_words)
    music = detect_music(args.video)
    print(f"music: {music.as_dict()}")
    if music.likelihood >= settings.get().music_threshold:
        print("  -> music detected")
    classifier = PolicyClassifier(key, settings, SpendLedger(make_session_factory(db_engine)))
    findings += classifier.check(args.video, probe(args.video).duration or 0.0, args.said)
    db_engine.dispose()
    if not findings:
        print("content: nothing found")
    for finding in findings:
        print(f"content: [{finding.verdict}] {finding.check}: {finding.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
