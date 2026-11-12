"""A fake Google/YouTube server for tests, shaped like the documented endpoints:
token exchange and refresh, channels, resumable upload sessions, playlistItems, videos.

`faults` is a list of one-shot failures to inject, consumed in order:
  "drop_after_complete"  finish the upload on the server, but drop the connection
  "drop_mid_upload"      accept a chunk, then drop the connection
  "expire_session"       forget the session (as Google does after a while)
  "expire_after_complete" finish the upload, drop the connection AND forget the session
  "server_error"         answer 503 once
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime

import httpx

CLIENT = ("google-client-id", "google-client-secret")
REFRESH = "rt-refresh-token-DO-NOT-LEAK"


class FakeYouTube:
    def __init__(self, now=lambda: datetime.now(UTC)) -> None:
        self.now = now
        self.videos: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.faults: list[str] = []
        self.calls: list[httpx.Request] = []
        self.challenge: str | None = None  # the PKCE challenge the "browser" presented
        self.refresh_valid = True
        self.access_serial = 0
        self.revoked: list[str] = []
        self.quota_exceeded = False
        self.reject_metadata = False

    # -- helpers ----------------------------------------------------------------------

    def _fault(self, name: str) -> bool:
        if self.faults and self.faults[0] == name:
            self.faults.pop(0)
            return True
        return False

    def _finish(self, session: dict) -> str:
        video_id = f"vid{len(self.videos) + 1:03d}"
        self.videos[video_id] = {
            "title": session["meta"]["snippet"]["title"],
            "description": session["meta"]["snippet"]["description"],
            "privacy": session["meta"]["status"]["privacyStatus"],
            "size": session["size"],
            "published": self.now(),
            "upload_status": "processed",
            "rejection_reason": None,
            "views": 12,
        }
        session["video_id"] = video_id
        return video_id

    def upload_count(self) -> int:
        return len(self.videos)

    # -- the server -------------------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        host, path = request.url.host, request.url.path
        if host == "oauth2.googleapis.com":
            return self._oauth(request, path)
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer at-{self.access_serial}":
            return httpx.Response(401, json={"error": {"errors": [{"reason": "authError"}]}})
        if self._fault("server_error"):
            return httpx.Response(503)
        if path == "/upload/youtube/v3/videos":
            if "upload_id" in request.url.params:
                return self._upload_put(request)
            return self._upload_start(request)
        if path == "/youtube/v3/channels":
            item = {
                "id": "UC1",
                "snippet": {"title": "My Clips"},
                "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}},
            }
            return httpx.Response(200, json={"items": [item]})
        if path == "/youtube/v3/playlistItems":
            items = [
                {
                    "snippet": {
                        "title": video["title"],
                        "publishedAt": video["published"].isoformat().replace("+00:00", "Z"),
                        "resourceId": {"videoId": video_id},
                    }
                }
                for video_id, video in sorted(self.videos.items(), reverse=True)
            ]
            return httpx.Response(200, json={"items": items})
        if path == "/youtube/v3/videos" and request.method == "GET":
            ids = request.url.params["id"].split(",")
            items = [
                {
                    "id": video_id,
                    "status": {
                        "uploadStatus": self.videos[video_id]["upload_status"],
                        "rejectionReason": self.videos[video_id]["rejection_reason"],
                        "privacyStatus": self.videos[video_id]["privacy"],
                    },
                    "statistics": {
                        "viewCount": str(self.videos[video_id]["views"]),
                        "likeCount": "3",
                        "commentCount": "1",
                    },
                }
                for video_id in ids
                if video_id in self.videos
            ]
            return httpx.Response(200, json={"items": items})
        if path == "/youtube/v3/videos" and request.method == "DELETE":
            return httpx.Response(204 if self.videos.pop(request.url.params["id"], None) else 404)
        return httpx.Response(404)

    def _oauth(self, request: httpx.Request, path: str) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        if path == "/revoke":
            self.revoked.append(form["token"])
            return httpx.Response(200)
        assert path == "/token"
        if (form.get("client_id"), form.get("client_secret")) != CLIENT:
            return httpx.Response(401, json={"error": "invalid_client"})
        if form["grant_type"] == "authorization_code":
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if form["code"] != "good-code" or challenge != self.challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            assert form["redirect_uri"].startswith("http://127.0.0.1:")
            self.access_serial += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"at-{self.access_serial}",
                    "refresh_token": REFRESH,
                    "expires_in": 3599,
                    "token_type": "Bearer",
                },
            )
        if form["grant_type"] == "refresh_token":
            if form["refresh_token"] != REFRESH or not self.refresh_valid:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.access_serial += 1
            return httpx.Response(
                200, json={"access_token": f"at-{self.access_serial}", "expires_in": 3599}
            )
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _upload_start(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params["uploadType"] == "resumable"
        assert request.url.params["part"] == "snippet,status"
        if self.quota_exceeded:
            body = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
            return httpx.Response(403, json=body)
        if self.reject_metadata:
            body = {"error": {"errors": [{"reason": "invalidTitle"}]}}
            return httpx.Response(400, json=body)
        meta = json.loads(request.content)
        assert "<" not in meta["snippet"]["title"] and len(meta["snippet"]["title"]) <= 100
        upload_id = f"sess{len(self.sessions) + 1}"
        url = f"https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id={upload_id}"
        self.sessions[upload_id] = {
            "size": int(request.headers["X-Upload-Content-Length"]),
            "received": 0,
            "meta": meta,
            "video_id": None,
        }
        return httpx.Response(200, headers={"Location": url})

    def _upload_put(self, request: httpx.Request) -> httpx.Response:
        upload_id = request.url.params["upload_id"]
        if self._fault("expire_session"):
            self.sessions.pop(upload_id, None)
        session = self.sessions.get(upload_id)
        if session is None:
            return httpx.Response(404)
        content_range = request.headers["Content-Range"]
        if content_range.startswith("bytes */"):  # "what do you have?"
            if session["video_id"]:
                return httpx.Response(200, json={"id": session["video_id"]})
            headers = {"Range": f"bytes=0-{session['received'] - 1}"} if session["received"] else {}
            return httpx.Response(308, headers=headers)
        span, total = content_range.removeprefix("bytes ").split("/")
        first, last = (int(x) for x in span.split("-"))
        assert int(total) == session["size"] and first == session["received"]
        assert len(request.content) == last - first + 1
        session["received"] = last + 1
        if self._fault("drop_mid_upload"):
            raise httpx.ReadError("connection reset", request=request)
        if session["received"] < session["size"]:
            return httpx.Response(308, headers={"Range": f"bytes=0-{session['received'] - 1}"})
        video_id = self._finish(session)
        if self._fault("drop_after_complete"):
            raise httpx.ReadError("connection reset", request=request)
        if self._fault("expire_after_complete"):
            del self.sessions[upload_id]
            raise httpx.ReadError("connection reset", request=request)
        return httpx.Response(200, json={"id": video_id})
