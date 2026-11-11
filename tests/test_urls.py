"""URL parsing: Phase 1 check. Accept real channel URLs in any common form; reject the rest."""

import pytest

from engine.monitor.urls import InvalidStreamerUrl, parse_streamer_url

GOOD = [
    # plain
    ("https://www.twitch.tv/shroud", "twitch", "shroud"),
    ("https://twitch.tv/shroud", "twitch", "shroud"),
    ("https://kick.com/xqc", "kick", "xqc"),
    # www. and m.
    ("https://m.twitch.tv/shroud", "twitch", "shroud"),
    ("https://www.kick.com/xqc", "kick", "xqc"),
    ("https://m.kick.com/xqc", "kick", "xqc"),
    ("https://go.twitch.tv/shroud", "twitch", "shroud"),
    # no scheme, http, upper case, whitespace around
    ("twitch.tv/shroud", "twitch", "shroud"),
    ("www.kick.com/xqc", "kick", "xqc"),
    ("http://twitch.tv/shroud", "twitch", "shroud"),
    ("HTTPS://WWW.TWITCH.TV/Shroud", "twitch", "shroud"),
    ("  https://kick.com/XQC \n", "kick", "xqc"),
    ("//twitch.tv/shroud", "twitch", "shroud"),
    # trailing paths, slashes, queries, fragments, ports
    ("https://www.twitch.tv/shroud/", "twitch", "shroud"),
    ("https://www.twitch.tv/shroud/videos", "twitch", "shroud"),
    ("https://www.twitch.tv/shroud/clip/FunnyClipSlug-abc123", "twitch", "shroud"),
    ("https://www.twitch.tv/shroud/about?tt_content=x&tt_medium=y", "twitch", "shroud"),
    ("https://www.twitch.tv/shroud?referrer=raid#chat", "twitch", "shroud"),
    ("https://twitch.tv:443/shroud", "twitch", "shroud"),
    ("https://kick.com/xqc/videos/0a1b2c3d", "kick", "xqc"),
    ("https://kick.com/xqc/clips/clip_01ABC", "kick", "xqc"),
    ("https://kick.com/xqc?ref=share", "kick", "xqc"),
    ("https://twitch.tv./shroud", "twitch", "shroud"),
    # popout / moderator / embedded player forms
    ("https://www.twitch.tv/popout/shroud/chat?popout=", "twitch", "shroud"),
    ("https://www.twitch.tv/moderator/shroud", "twitch", "shroud"),
    ("https://kick.com/popout/xqc/chat", "kick", "xqc"),
    ("https://player.twitch.tv/?channel=shroud&parent=example.com", "twitch", "shroud"),
    # name shapes
    ("https://twitch.tv/a_b_c_123", "twitch", "a_b_c_123"),
    ("https://twitch.tv/" + "a" * 25, "twitch", "a" * 25),
    ("https://kick.com/some-name_1", "kick", "some-name_1"),
    ("https://kick.com/" + "k" * 25, "kick", "k" * 25),
]

BAD = [
    "",
    "   ",
    "shroud",  # no platform
    "not a url at all",
    "https://",
    "https://www.twitch.tv",  # no channel
    "https://www.twitch.tv/",
    "https://kick.com",
    "https://www.youtube.com/@someone",  # unsupported platform
    "https://twitch.com/shroud",  # wrong domain
    "https://twitch.tv.evil.com/shroud",  # lookalike hosts
    "https://eviltwitch.tv/shroud",
    "https://kick.com.evil.io/xqc",
    "https://twitch.tv@evil.com/shroud",  # userinfo trick: real host is evil.com
    "https://evil.com/twitch.tv/shroud",
    "https://evil.com/?u=https://twitch.tv/shroud",
    "javascript:alert(1)",
    "ftp://twitch.tv/shroud",
    "file:///C:/Users/me/twitch.tv/shroud",
    "https://www.twitch.tv/directory/category/grand-theft-auto-vi",  # site pages
    "https://www.twitch.tv/videos/2301234567",
    "https://www.twitch.tv/settings/profile",
    "https://clips.twitch.tv/FunnyClipSlug-abc123",
    "https://kick.com/browse",
    "https://kick.com/categories/gta-vi",
    "https://kick.com/video/0a1b2c3d-4e5f",
    "https://player.twitch.tv/?video=v123",  # player without a channel
    "https://www.twitch.tv/popout",  # wrapper with nothing after it
    "https://twitch.tv/" + "a" * 26,  # too long
    "https://kick.com/" + "k" * 26,
    "https://twitch.tv/has-hyphen",  # not allowed in Twitch logins
    "https://twitch.tv/naïve",
    "https://twitch.tv/%2e%2e",
    "https://twitch.tv/..",
    "https://twitch.tv/a b",
    "https://twitch.tv/shroud\x00",
    "https://[::1/shroud",  # malformed
    "https://twitch.tv/" + "a" * 3000,
]


@pytest.mark.parametrize(("text", "platform", "username"), GOOD)
def test_accepts(text, platform, username):
    ref = parse_streamer_url(text)
    assert (ref.platform, ref.username) == (platform, username)


@pytest.mark.parametrize("text", BAD)
def test_rejects(text):
    with pytest.raises(InvalidStreamerUrl) as excinfo:
        parse_streamer_url(text)
    assert str(excinfo.value)  # always a message a person can act on


def test_rejects_non_string_input():
    with pytest.raises(InvalidStreamerUrl):
        parse_streamer_url(None)  # type: ignore[arg-type]


def test_canonical_url_round_trips():
    for text, _platform, _username in GOOD:
        ref = parse_streamer_url(text)
        assert parse_streamer_url(ref.url) == ref


def test_messages_guide_the_user():
    with pytest.raises(InvalidStreamerUrl, match="clip link"):
        parse_streamer_url("https://clips.twitch.tv/Abc")
    with pytest.raises(InvalidStreamerUrl, match="full channel URL"):
        parse_streamer_url("shroud")
    with pytest.raises(InvalidStreamerUrl, match="Only twitch.tv and kick.com"):
        parse_streamer_url("https://youtube.com/@x")
