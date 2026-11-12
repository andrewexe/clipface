"""Policy gate. Includes the Phase 7 check: a set of bad clips (music, slurs, addresses
on screen, and more) is 100% blocked or flagged, under the most permissive settings.

What the classifier "sees" is scripted here: these tests prove the gate acts correctly on
every classifier answer and on every classifier failure. They cannot prove that Claude
itself spots an address in a frame; `tests/make_policy_samples.py` makes sample clips for
checking that with a real key.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import anthropic
import httpx
import numpy as np
import pytest
from sqlalchemy import select

from engine.control.spend import SpendLedger
from engine.db.models import AuditLog, Clip, Moment, Streamer, StreamSession, utcnow
from engine.db.session import create_db_engine, make_session_factory, run_migrations
from engine.policy.classifier import PolicyClassifier, PolicyReport, extract_frames, report_findings
from engine.policy.gate import PolicyGate, autonomy_blockers
from engine.policy.music import analyse, detect_music
from engine.policy.rules import build_description, credit_line, normalise_rules
from engine.policy.text import blocked_word_count, check_text
from engine.settings import AppSettings, SettingsStore
from tests import audio_samples
from tests.test_scorer import reply

SENTINEL = "clipperblocklisttest"  # stands in for a real slur: it is on the built-in list


def checks(text, **kwargs):
    return {(f.check, f.verdict) for f in check_text(text, **kwargs)}


# -- local text checks ------------------------------------------------------------------


def test_blocked_words_built_in_list_and_disguises():
    assert blocked_word_count(f"what a {SENTINEL}") == 1
    assert blocked_word_count(f"{SENTINEL.upper()}S!!") == 1  # plural, capitals, punctuation
    assert blocked_word_count(SENTINEL.replace("i", "1").replace("e", "3")) == 1  # leetspeak
    assert blocked_word_count(SENTINEL.replace("i", "iiii")) == 1  # stretched
    assert blocked_word_count("a perfectly normal sentence about the game") == 0
    # Innocent words that resemble entries on the list are left alone.
    assert blocked_word_count("Niger is a country, snigger, retarding the spark, a raccoon") == 0


def test_user_blocked_words():
    assert blocked_word_count("you absolute Dingbat!!", ["dingbat"]) == 1
    assert blocked_word_count("dingbats everywhere", ["dingbat"]) == 1
    assert blocked_word_count("nothing here", ["dingbat", "", "  "]) == 0


@pytest.mark.parametrize(
    "text",
    [
        "call me on 415-555-0142 tonight",
        "my number is +44 7700 900123",
        "the card is 4111 1111 1111 1111",
        "email me at some.person@example.com",
        "it's 123-45-6789",
        "I live at 221 Baker Street",
        "he lives at 1600 Pennsylvania Avenue apparently",
        "her home address is right there on screen",
    ],
)
def test_personal_details_in_speech_are_blocked(text):
    assert ("personal_info", "block") in checks(text)


@pytest.mark.parametrize(
    "text",
    [
        "score was 10 to 20 and we got 1,000,000 points in 2026",
        "that took 45 minutes, 3 tries, round 12",
        "version 1.24.3 dropped at 60 fps",
        "we need 5000 more subs for the goal",
    ],
)
def test_ordinary_numbers_are_not_personal_details(text):
    assert checks(text) == set()


def test_gambling_promotion_is_blocked_and_mentions_are_flagged():
    assert ("gambling_promotion", "block") in checks("use code SHROUD on Stake for free spins")
    assert ("gambling_promotion", "block") in checks(
        "sign up at the casino, link in the description"
    )
    assert checks("we went to the casino last night") == {("gambling", "flag")}
    assert checks("put your money where your mouth is") == set()


def test_program_banned_terms_are_flagged():
    assert checks("talking about Rival Game Two", banned=["rival game"]) == {
        ("program_rules", "flag")
    }
    assert checks("nothing banned here", banned=["rival game", ""]) == set()
    assert checks("") == set()


# -- program rules and credit -------------------------------------------------------------


def test_credit_always_names_the_streamer_and_links_the_channel():
    url = "https://www.twitch.tv/shroud"
    assert credit_line("shroud", url) == f"Clip from shroud: {url}"
    custom = credit_line("shroud", url, {"credit_format": "Watch {name} live at {url}!"})
    assert custom == f"Watch shroud live at {url}!"
    # A format that drops the link or the name, or is broken, still ends up with both.
    for bad in ("Great clip", "{name} only", "{nope} {url}", "{name", "{0}"):
        line = credit_line("shroud", url, {"credit_format": bad})
        assert url in line and "shroud" in line


def test_description_adds_missing_required_tags_then_credit():
    rules = {"required_tags": ["gta6", "#ShroudClips", "bad tag!"], "credit_format": ""}
    text = build_description("He did it #GTA6", "shroud", "https://www.twitch.tv/shroud", rules)
    assert (
        text == "He did it #GTA6\n\n#ShroudClips\n\nClip from shroud: https://www.twitch.tv/shroud"
    )
    assert build_description(None, "x", "https://kick.com/x") == "Clip from x: https://kick.com/x"


def test_rules_are_cleaned_and_bounded():
    rules = normalise_rules({
        "required_tags": ["a", "a", "#b", 7, None, "has space", "x" * 99] + [f"t{i}" for i in range(40)],
        "credit_format": "  spaced   out  " + "y" * 500,
        "banned": ["  slots ", "slots", {"x": 1}], "junk": "ignored",
    })  # fmt: skip
    assert rules["required_tags"][:3] == ["#a", "#b", "#7"] and len(rules["required_tags"]) == 15
    assert len(rules["credit_format"]) == 200 and rules["banned"] == ["slots"]
    assert (
        normalise_rules(None)
        == normalise_rules("nonsense")
        == {"required_tags": [], "credit_format": "", "banned": []}
    )


# -- music detector -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "samples",
    [
        audio_samples.song(seed=1),
        audio_samples.song(bpm=90, seed=2),
        audio_samples.song(drums=False, seed=3),
    ],
)
def test_music_is_detected(samples):
    assert analyse(samples).likelihood >= 0.9


def test_noise_and_silence_are_not_music():
    assert analyse(audio_samples.noise()).likelihood < 0.2  # periodic, but no held notes
    assert analyse(np.zeros(8000 * 10, dtype=np.float32)).likelihood == 0.0
    assert analyse(np.zeros(100, dtype=np.float32)).likelihood == 0.0  # too short to tell


def test_speech_is_not_music_but_speech_over_music_is(tmp_path):
    text = (
        "No way, he actually did it! Look at that. Chat, did you see what just happened? "
        "That was the craziest thing I have ever seen in this game. Okay, let's go again."
    )
    male = audio_samples.speech(tmp_path / "m.wav", text)
    if male is None:
        pytest.skip("espeak-ng is not installed")
    female = audio_samples.speech(tmp_path / "f.wav", text, voice="en-us+f3", speed=190)
    assert analyse(male).likelihood < 0.5
    assert analyse(female).likelihood < 0.5
    bed = audio_samples.song(seconds=len(male) / 8000, seed=4)
    assert analyse(male + 1.0 * bed[: len(male)]).likelihood >= 0.5
    assert analyse(male + 0.25 * bed[: len(male)]).likelihood >= 0.5  # quiet background music


def test_detect_music_reads_real_files(tmp_path):
    music = audio_samples.clip_with_audio(tmp_path / "music.mp4", audio_samples.song(seconds=8))
    assert detect_music(music).likelihood >= 0.9
    assert detect_music(tmp_path / "missing.mp4").likelihood == 0.0


# -- classifier ---------------------------------------------------------------------------


def report(**levels):
    base = dict.fromkeys(
        ("sexual_content", "slurs_or_hate", "minor_on_screen", "personal_info",
         "private_messages", "gambling_promotion", "abusive_donation_or_tts"), "no")  # fmt: skip
    notes = levels.pop("notes", [])
    return PolicyReport(**{**base, **levels}, notes=notes)


class ScriptedClient:
    """client.beta.messages.parse(...) returns (or raises) whatever `outcome` is."""

    def __init__(self, outcome=None):
        self.outcome = outcome if outcome is not None else reply(report())
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def api_error(cls, status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    root = tmp_path_factory.mktemp("policy-clips")
    return SimpleNamespace(
        clean=audio_samples.clip_with_audio(
            root / "clean.mp4", audio_samples.noise(seconds=8) * 0.3
        ),
        music=audio_samples.clip_with_audio(root / "music.mp4", audio_samples.song(seconds=8)),
        address=audio_samples.clip_with_audio(
            root / "address.mp4",
            audio_samples.noise(seconds=8) * 0.3,
            text="221 Baker Street, London",
        ),
    )


@pytest.fixture
def env(tmp_path, clips):
    db_engine = create_db_engine(tmp_path / "p.db")
    run_migrations(db_engine)
    factory = make_session_factory(db_engine)
    settings = SettingsStore(tmp_path / "settings.json")
    # The most permissive setup: autonomy on, permission granted, a high score.
    settings.update({"autonomy_level": "L2"})
    with factory.begin() as db:
        db.add(
            Streamer(
                platform="twitch",
                username="shroud",
                display_name="shroud",
                permission_status="granted",
            )
        )
        db.flush()
        db.add(StreamSession(streamer_id=1, started_at=utcnow()))
        db.flush()
        db.add(Moment(session_id=1, peak_ts=utcnow(), status="clipped"))
        db.flush()
        db.add(Clip(moment_id=1, start_ts=utcnow(), end_ts=utcnow() + timedelta(seconds=8),
                    title="He actually did it", caption="#gta6", transcript="no way he did that",
                    claude_score=0.95, status="pending", file_path=str(clips.clean),
                    meta={"duration": 8.0}))  # fmt: skip
    state = SimpleNamespace(client=ScriptedClient(), key="sk-ant-test", roll=0.99)
    ledger = SpendLedger(factory)
    classifier = PolicyClassifier(
        lambda: state.key, settings, ledger, client_factory=lambda k: state.client
    )
    gate = PolicyGate(factory, settings, classifier=classifier, rng=lambda: state.roll)

    def clip():
        with factory() as db:
            return db.get(Clip, 1)

    def edit(**fields):
        with factory.begin() as db:
            row = db.get(Clip, 1)
            for key, value in fields.items():
                setattr(row, key, value)

    def audit():
        with factory() as db:
            return [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]

    yield SimpleNamespace(factory=factory, settings=settings, gate=gate, state=state, clips=clips,
                          clip=clip, edit=edit, audit=audit, classifier=classifier)  # fmt: skip
    db_engine.dispose()


def test_frames_are_extracted_in_order(clips):
    frames = extract_frames(clips.clean, 8.0)
    assert len(frames) == 5 and all(frame[:2] == b"\xff\xd8" for frame in frames)  # JPEGs
    assert extract_frames(clips.clean.with_name("missing.mp4"), 8.0) == []


def test_classifier_request_shape(env):
    findings = env.classifier.check(env.clips.clean, 8.0, "said <b>something</b>")
    assert findings == []
    (call,) = env.state.client.calls
    assert call["model"] == "claude-opus-5-5" and call["output_format"] is PolicyReport
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
    content = call["messages"][0]["content"]
    images = [block for block in content if block["type"] == "image"]
    assert len(images) == 5 and all(i["source"]["media_type"] == "image/jpeg" for i in images)
    last = content[-1]["text"]
    assert "<transcript>" in last and "<b>" not in last  # tags in speech can't close ours
    assert "untrusted" in call["system"] and "unsure" in call["system"]


def test_report_levels_map_to_verdicts():
    findings = report_findings(
        report(personal_info="yes", minor_on_screen="unsure", notes=["an address is visible"])
    )
    got = {(f.check, f.verdict) for f in findings}
    assert got == {
        ("personal_info", "block"),
        ("minor_on_screen", "flag"),
        ("classifier_notes", "flag"),
    }
    assert report_findings(report(notes=["all fine"])) == []  # notes alone are not a finding


# -- gate decisions -----------------------------------------------------------------------


def test_clean_clip_is_auto_approved_under_l2(env):
    """The control case: with nothing wrong, this setup does publish on its own. Every
    bad clip below runs under exactly these settings."""
    heard = []
    env.gate.on_approved(heard.append)
    assert env.gate.review(1) == "auto_approved"
    clip = env.clip()
    assert clip.status == "approved" and clip.meta["auto_approved"] is True
    assert clip.policy_result["verdict"] == "clean" and clip.policy_result["findings"] == []
    assert clip.policy_result["music"]["likelihood"] < 0.2
    assert heard == [1] and env.audit() == ["clip.auto_approve"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda e: e.settings.update({"autonomy_level": "L1"}), "autonomy is L1"),
        (lambda e: e.settings.update({"publishing_paused": True}), "publishing is paused"),
        (lambda e: e.edit(claude_score=0.6), "below the auto-approve level"),
        (lambda e: e.edit(claude_score=None), "was not scored"),
        (lambda e: setattr(e.state, "roll", 0.05), "picked at random"),
    ],
)
def test_clean_clip_still_needs_a_person_when(env, change, reason):
    heard = []
    env.gate.on_needs_review(heard.append)
    change(env)
    assert env.gate.review(1) == "review"
    clip = env.clip()
    assert clip.status == "pending" and clip.policy_result["verdict"] == "clean"
    assert any(reason in why for why in clip.policy_result["decision"]["why"])
    assert heard == [1]


@pytest.mark.parametrize("status", ["none", "asked"])
def test_no_auto_publish_without_the_streamers_permission(env, status):
    with env.factory.begin() as db:
        db.get(Streamer, 1).permission_status = status
    assert env.gate.review(1) == "review"
    assert "no permission" in " ".join(env.clip().policy_result["decision"]["why"])


def test_program_permission_counts_and_sampled_flag_is_recorded(env):
    with env.factory.begin() as db:
        db.get(Streamer, 1).permission_status = "program"
    env.state.roll = 0.0999
    assert env.gate.review(1) == "review" and env.clip().policy_result["sampled"] is True
    env.edit(status="pending", policy_result=None)
    env.state.roll = 0.1001
    assert env.gate.review(1) == "auto_approved"


def test_autonomy_blockers_lists_every_reason():
    streamer = SimpleNamespace(permission_status="none", enabled=False)
    reasons = autonomy_blockers(AppSettings(publishing_paused=True), streamer, None)
    assert len(reasons) == 5
    ok = SimpleNamespace(permission_status="granted", enabled=True)
    assert autonomy_blockers(AppSettings(autonomy_level="L2"), ok, 0.8) == []


# -- the Phase 7 check: bad clips ---------------------------------------------------------

BAD_CLIPS = {
    "music bed": lambda e: e.edit(file_path=str(e.clips.music)),
    "slur said": lambda e: e.edit(transcript=f"you absolute {SENTINEL}"),
    "slur disguised": lambda e: e.edit(
        transcript=SENTINEL.replace("i", "1").replace("e", "3").upper() + "!!!"
    ),
    "slur in the title": lambda e: e.edit(title=f"{SENTINEL} moment"),
    "own blocked word": lambda e: (
        e.settings.update({"blocked_words": ["dingbat"]}),
        e.edit(transcript="what a dingbat"),
    ),
    "phone number said": lambda e: e.edit(transcript="just call 415-555-0142 okay"),
    "street address said": lambda e: e.edit(transcript="yeah I live at 221 Baker Street"),
    "gambling promo": lambda e: e.edit(transcript="use code SHROUD on stake for a deposit bonus"),
    "gambling mention": lambda e: e.edit(transcript="spinning slots all night"),
    "program banned topic": lambda e: e.edit(transcript="over on rival game two"),
    "address on screen": lambda e: (
        e.edit(file_path=str(e.clips.address)),
        setattr(
            e.state.client,
            "outcome",
            reply(report(personal_info="yes", notes=["a street address is on screen"])),
        ),
    ),
    "nudity": lambda e: setattr(e.state.client, "outcome", reply(report(sexual_content="yes"))),
    "hate speech (classifier)": lambda e: setattr(
        e.state.client, "outcome", reply(report(slurs_or_hate="yes"))
    ),
    "minor possibly on screen": lambda e: setattr(
        e.state.client, "outcome", reply(report(minor_on_screen="unsure"))
    ),
    "minor on screen": lambda e: setattr(
        e.state.client, "outcome", reply(report(minor_on_screen="yes"))
    ),
    "DMs on screen": lambda e: setattr(
        e.state.client, "outcome", reply(report(private_messages="yes"))
    ),
    "abusive TTS": lambda e: setattr(
        e.state.client, "outcome", reply(report(abusive_donation_or_tts="yes"))
    ),
    "gambling promo on screen": lambda e: setattr(
        e.state.client, "outcome", reply(report(gambling_promotion="unsure"))
    ),
    # Classifier failures must fail closed.
    "no API key": lambda e: setattr(e.state, "key", None),
    "budget spent": lambda e: e.settings.update({"anthropic_daily_cap_usd": 0}),
    "API down": lambda e: setattr(
        e.state.client, "outcome", api_error(anthropic.InternalServerError, 500)
    ),
    "key rejected": lambda e: setattr(
        e.state.client, "outcome", api_error(anthropic.AuthenticationError, 401)
    ),
    "classifier declined": lambda e: setattr(
        e.state.client, "outcome", reply(None, stop_reason="refusal")
    ),
    "unreadable reply": lambda e: setattr(e.state.client, "outcome", reply({"oops": 1})),
    "reply parse error": lambda e: setattr(e.state.client, "outcome", ValueError("bad json")),
    "video file missing": lambda e: e.edit(file_path="/nonexistent/clip.mp4"),
    "music check crashes": lambda e: setattr(e.gate, "_music", lambda path: 1 / 0),
}


@pytest.mark.parametrize("name", list(BAD_CLIPS))
def test_bad_clips_are_never_auto_approved(env, name):
    approved = []
    env.gate.on_approved(approved.append)
    with env.factory.begin() as db:
        db.get(Streamer, 1).program_rules = {"banned": ["rival game"]}
    BAD_CLIPS[name](env)

    action = env.gate.review(1)

    clip = env.clip()
    assert action in ("rejected", "review"), name
    assert clip.status in ("rejected", "pending") and approved == []
    assert clip.policy_result["verdict"] in ("block", "flag")
    assert clip.policy_result["findings"], "the reason must be recorded for the reviewer"
    assert not (clip.meta or {}).get("auto_approved")


@pytest.mark.parametrize(
    "name",
    ["slur said", "slur disguised", "phone number said", "street address said", "gambling promo",
     "address on screen", "nudity", "hate speech (classifier)", "minor on screen", "DMs on screen",
     "abusive TTS"],
)  # fmt: skip
def test_clear_violations_are_rejected_outright(env, name):
    BAD_CLIPS[name](env)
    assert env.gate.review(1) == "rejected"
    clip = env.clip()
    assert clip.status == "rejected" and clip.policy_result["verdict"] == "block"
    assert env.audit() == ["clip.policy_reject"]


def test_music_can_be_set_to_reject(env):
    env.edit(file_path=str(env.clips.music))
    assert env.gate.review(1) == "review"
    assert ("music", "flag") in {
        (f["check"], f["verdict"]) for f in env.clip().policy_result["findings"]
    }
    env.edit(status="pending")
    env.settings.update({"music_action": "reject"})
    assert env.gate.review(1) == "rejected"


def test_gate_only_acts_on_pending_clips(env):
    env.edit(status="approved")
    assert env.gate.review(1) == "review" and env.clip().policy_result is None
    assert env.gate.review(999) == "review"  # unknown clip: nothing to do, nothing raised
