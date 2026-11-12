"""YouTube Data API v3 client: installed-app OAuth, resumable uploads, status, delete.

Only official endpoints:
  auth      https://accounts.google.com/o/oauth2/v2/auth  (loopback redirect + PKCE)
  token     https://oauth2.googleapis.com/token
  upload    https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable
  data      https://www.googleapis.com/youtube/v3/...

Uploading without duplicates. A resumable upload has a session URL. It is saved before
any bytes are sent, so a retry (after a dropped connection, a crash, a restart) asks that
session what it already has: finished -> take the video id; partly there -> continue from
that byte; gone -> look for a recent upload with the same title on the channel before
starting again.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets as pysecrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from engine.publish.errors import PublishAuthError, PublishError, PublishRejected

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
# upload: post videos. force-ssl: read the channel and video status, and delete a video
# (needed to take clips down when a streamer asks).
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
)
CHUNK = 8 * 1024 * 1024
CLOCK_SKEW = timedelta(minutes=15)
GAMING_CATEGORY = "20"
TITLE_MAX = 100
DESCRIPTION_MAX = 4900


class YouTubeError(PublishError):
    """A temporary problem: try again later."""


class YouTubeAuthError(YouTubeError, PublishAuthError):
    """The account's sign-in no longer works; the user has to reconnect it."""


class YouTubeRejected(YouTubeError, PublishRejected):
    """YouTube refused this upload for good (bad metadata, forbidden)."""


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str
    state: str


def new_pkce() -> Pkce:
    verifier = pysecrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return Pkce(verifier=verifier, challenge=challenge, state=pysecrets.token_urlsafe(24))


def authorization_url(client_id: str, redirect_uri: str, pkce: Pkce) -> str:
    return (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "code_challenge": pkce.challenge,
                "code_challenge_method": "S256",
                "state": pkce.state,
                "access_type": "offline",  # we need a refresh token
                "prompt": "consent",
            }
        )
    )


def clean_metadata(title: str, description: str) -> tuple[str, str]:
    """YouTube rejects angle brackets, and limits lengths."""

    def strip(text: str) -> str:
        return text.replace("<", "").replace(">", "")

    title = " ".join(strip(title).split())[:TITLE_MAX].strip() or "Clip"
    return title, strip(description)[:DESCRIPTION_MAX]


def _error_reason(response: httpx.Response) -> str | None:
    try:
        errors = response.json()["error"]["errors"]
        return str(errors[0].get("reason")) if errors else None
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None


class YouTubeClient:
    def __init__(
        self,
        client_credentials: Callable[[], tuple[str, str] | None],
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client_credentials = client_credentials
        self._http = httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), transport=transport)
        self._access: dict[str, tuple[str, float]] = {}  # refresh token -> (access, expiry)

    def close(self) -> None:
        self._http.close()

    # -- OAuth ----------------------------------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        creds = self._client_credentials()
        if not creds or not all(creds):
            raise YouTubeAuthError("the Google client ID and secret are not set")
        return creds

    def exchange_code(self, code: str, redirect_uri: str, pkce: Pkce) -> dict[str, Any]:
        """Authorization code -> tokens. Returns the token response."""
        client_id, client_secret = self._credentials()
        response = self._post_token(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": pkce.verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            }
        )
        if not response.get("refresh_token"):
            raise YouTubeAuthError("Google did not return a refresh token; try connecting again")
        return response

    def _post_token(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._http.post(TOKEN_URL, data=data)
        except httpx.HTTPError as exc:
            raise YouTubeError(f"could not reach Google ({type(exc).__name__})") from None
        if response.status_code in (400, 401):
            raise YouTubeAuthError("Google rejected the sign-in; connect the account again")
        if response.status_code != 200:
            raise YouTubeError(f"Google sign-in returned {response.status_code}")
        try:
            body = response.json()
            if not isinstance(body.get("access_token"), str):
                raise ValueError
        except (ValueError, AttributeError):
            raise YouTubeError("Google sign-in sent an unreadable reply") from None
        return body

    def access_token(self, refresh_token: str) -> str:
        cached = self._access.get(refresh_token)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        client_id, client_secret = self._credentials()
        body = self._post_token(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        )
        expires = time.monotonic() + max(60.0, float(body.get("expires_in") or 3600) - 120)
        self._access[refresh_token] = (body["access_token"], expires)
        return body["access_token"]

    def revoke(self, refresh_token: str) -> None:
        """Best effort: tell Google to forget this grant."""
        try:
            self._http.post(REVOKE_URL, data={"token": refresh_token})
        except httpx.HTTPError:
            pass
        self._access.pop(refresh_token, None)

    # -- requests -------------------------------------------------------------------

    def _request(self, method: str, url: str, refresh_token: str, **kwargs: Any) -> httpx.Response:
        headers = {**kwargs.pop("headers", {})}
        for attempt in (1, 2):
            headers["Authorization"] = f"Bearer {self.access_token(refresh_token)}"
            try:
                response = self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise YouTubeError(f"could not reach YouTube ({type(exc).__name__})") from None
            if response.status_code == 401 and attempt == 1:
                self._access.pop(refresh_token, None)  # stale access token: refresh once
                continue
            return response
        raise AssertionError("unreachable")

    def _check(self, response: httpx.Response, what: str) -> None:
        status = response.status_code
        if status < 300:
            return
        reason = _error_reason(response)
        if status == 401:
            raise YouTubeAuthError("YouTube no longer accepts this account's sign-in")
        if status == 403 and reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            raise YouTubeError("YouTube's daily upload quota is used up")
        if status in (429, 500, 502, 503, 504):
            raise YouTubeError(f"YouTube returned {status} for {what}")
        raise YouTubeRejected(f"YouTube refused {what} ({status}: {reason or 'no reason'})", reason)

    def channel(self, refresh_token: str) -> dict[str, str]:
        """The signed-in channel: id, title, and its uploads playlist."""
        response = self._request(
            "GET",
            f"{API}/channels",
            refresh_token,
            params={"part": "snippet,contentDetails", "mine": "true"},
        )
        self._check(response, "reading the channel")
        try:
            item = response.json()["items"][0]
            return {
                "id": item["id"],
                "title": item["snippet"]["title"],
                "uploads": item["contentDetails"]["relatedPlaylists"]["uploads"],
            }
        except (ValueError, KeyError, IndexError, TypeError):
            raise YouTubeRejected("this Google account has no YouTube channel") from None

    # -- upload ---------------------------------------------------------------------

    def start_upload(
        self,
        refresh_token: str,
        size: int,
        title: str,
        description: str,
        privacy: str,
        tags: list[str] | None = None,
    ) -> str:
        """Open a resumable session and return its URL. No video bytes are sent yet."""
        title, description = clean_metadata(title, description)
        response = self._request(
            "POST",
            UPLOAD_URL,
            refresh_token,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
            json={
                "snippet": {
                    "title": title,
                    "description": description,
                    "categoryId": GAMING_CATEGORY,
                    "tags": [t.lstrip("#")[:30] for t in (tags or [])][:15],
                },
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
            },
        )
        self._check(response, "starting the upload")
        session_url = response.headers.get("Location")
        if not session_url:
            raise YouTubeError("YouTube did not return an upload session")
        return session_url

    def upload_status(self, refresh_token: str, session_url: str, size: int) -> tuple[str, Any]:
        """Ask a session what it has. Returns ("done", video id), ("resume", next byte),
        or ("gone", None) when the session no longer exists."""
        response = self._request(
            "PUT", session_url, refresh_token, headers={"Content-Range": f"bytes */{size}"}
        )
        if response.status_code in (200, 201):
            return "done", self._video_id(response)
        if response.status_code == 308:
            sent = response.headers.get("Range")  # "bytes=0-12345", absent if nothing yet
            return "resume", int(sent.rsplit("-", 1)[1]) + 1 if sent else 0
        if response.status_code in (404, 410):
            return "gone", None
        self._check(response, "checking the upload")
        raise YouTubeError("unexpected reply while checking the upload")

    def send_bytes(self, refresh_token: str, session_url: str, path: Path, start: int = 0) -> str:
        """Send the file from `start` in chunks. Returns the new video's id."""
        size = path.stat().st_size
        with path.open("rb") as handle:
            position = start
            while position < size:
                handle.seek(position)
                chunk = handle.read(CHUNK)
                end = position + len(chunk) - 1
                response = self._request(
                    "PUT",
                    session_url,
                    refresh_token,
                    content=chunk,
                    headers={
                        "Content-Range": f"bytes {position}-{end}/{size}",
                        "Content-Type": "video/mp4",
                    },
                )
                if response.status_code in (200, 201):
                    return self._video_id(response)
                if response.status_code == 308:
                    sent = response.headers.get("Range")
                    position = int(sent.rsplit("-", 1)[1]) + 1 if sent else position
                    continue
                if response.status_code in (404, 410):
                    raise YouTubeError("the upload session expired")
                self._check(response, "uploading the video")
        raise YouTubeError("the upload ended without a video id")

    @staticmethod
    def _video_id(response: httpx.Response) -> str:
        try:
            video_id = response.json()["id"]
            if not isinstance(video_id, str) or not video_id:
                raise ValueError
            return video_id
        except (ValueError, KeyError, TypeError):
            raise YouTubeError("YouTube finished the upload but sent no video id") from None

    def find_recent_upload(
        self, refresh_token: str, uploads_playlist: str, title: str, since: datetime
    ) -> str | None:
        """A video on this channel with exactly this title, published after `since`."""
        wanted, _ = clean_metadata(title, "")
        response = self._request(
            "GET",
            f"{API}/playlistItems",
            refresh_token,
            params={"part": "snippet", "playlistId": uploads_playlist, "maxResults": "25"},
        )
        self._check(response, "listing recent uploads")
        try:
            items = response.json().get("items", [])
        except ValueError:
            return None
        for item in items:
            snippet = item.get("snippet", {}) if isinstance(item, dict) else {}
            try:
                published = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))
                video_id = snippet["resourceId"]["videoId"]
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if snippet.get("title") == wanted and published >= since:
                return video_id
        return None

    # -- after publishing -------------------------------------------------------------

    def video_states(self, refresh_token: str, video_ids: list[str]) -> dict[str, dict]:
        """Upload status and view counts for up to 50 videos. Missing ids were deleted."""
        if not video_ids:
            return {}
        response = self._request(
            "GET",
            f"{API}/videos",
            refresh_token,
            params={"part": "status,statistics", "id": ",".join(video_ids[:50])},
        )
        self._check(response, "reading video status")
        states = {}
        for item in response.json().get("items", []):
            status = item.get("status", {})
            stats = item.get("statistics", {})
            states[item.get("id")] = {
                "upload_status": status.get("uploadStatus"),
                "rejection_reason": status.get("rejectionReason"),
                "privacy": status.get("privacyStatus"),
                "views": int(stats.get("viewCount", 0) or 0),
                "likes": int(stats.get("likeCount", 0) or 0),
                "comments": int(stats.get("commentCount", 0) or 0),
            }
        return states

    def delete_video(self, refresh_token: str, video_id: str) -> bool:
        """Delete a video. True if it is gone (including if it was already gone)."""
        response = self._request("DELETE", f"{API}/videos", refresh_token, params={"id": video_id})
        if response.status_code in (204, 404):
            return True
        self._check(response, "deleting the video")
        return False


class YouTubePublisher:
    """The scheduler's view of YouTube: publish one clip, remove a post, read states."""

    platform = "youtube"

    def __init__(self, client: YouTubeClient, now: Callable[[], datetime]) -> None:
        self.client = client
        self._now = now

    def publish(
        self,
        token: str,
        state: dict,
        save_state: Callable[[dict], None],
        video: Path,
        title: str,
        description: str,
        tags: list[str],
        privacy: str,
    ) -> str:
        """Upload `video` and return the video id. Safe to call again after any failure:
        it continues or adopts the earlier attempt instead of uploading twice."""
        size = video.stat().st_size
        session_url = state.get("session_url")
        if session_url:
            kind, value = self.client.upload_status(token, session_url, size)
            if kind == "done":
                return value
            if kind == "resume":
                return self.client.send_bytes(token, session_url, video, start=value)
            # The session is gone. It may have finished first: look before uploading again.
            # Allow for this computer's clock and YouTube's disagreeing by a few minutes.
            started = datetime.fromisoformat(state["started_at"]) - CLOCK_SKEW
            uploads = self.client.channel(token)["uploads"]
            found = self.client.find_recent_upload(token, uploads, title, started)
            if found:
                return found
        session_url = self.client.start_upload(token, size, title, description, privacy, tags)
        # Saved before a single byte goes out, so a crash from here on can be resumed.
        save_state({"session_url": session_url, "started_at": self._now().isoformat()})
        return self.client.send_bytes(token, session_url, video)

    def remove(self, token: str, post_id: str) -> bool:
        return self.client.delete_video(token, post_id)

    def states(self, token: str, post_ids: list[str]) -> dict[str, dict]:
        return self.client.video_states(token, post_ids)

    @staticmethod
    def url(post_id: str) -> str:
        return f"https://youtu.be/{post_id}"
