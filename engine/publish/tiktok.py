"""TikTok Content Posting API: upload a clip to the account's TikTok inbox as a draft.

Why drafts and not direct posts. TikTok's rules for its direct-post API require the
account owner to pick the privacy level by hand for every post (no default), to see and
set the interaction options, and to consent before anything is sent. Apps that have not
passed TikTok's audit can also only post privately, and the audit is for apps with a
wide audience. An app that posts on its own cannot meet those rules. Uploading to the
inbox can: the clip arrives in TikTok as a notification, and the owner finishes and
posts it there.

Endpoints (open.tiktokapis.com):
  /v2/oauth/token/                         code exchange and refresh
  /v2/post/publish/inbox/video/init/       open an upload (scope video.upload)
  PUT <upload_url>                         send the file, in order, with Content-Range
  /v2/post/publish/status/fetch/           where the upload has got to
"""

from __future__ import annotations

import hashlib
import logging
import secrets as pysecrets
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from engine.publish.errors import (
    PublishAuthError,
    PublishError,
    PublishPending,
    PublishRejected,
)

log = logging.getLogger(__name__)

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com"
SCOPE = "video.upload"
SINGLE_CHUNK_MAX = 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024
UPLOAD_URL_LIFETIME = timedelta(minutes=55)  # TikTok: valid for one hour
DELIVERED = ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE")


def new_pkce() -> tuple[str, str, str]:
    """(verifier, challenge, state). TikTok wants the challenge as hex SHA-256."""
    verifier = pysecrets.token_urlsafe(48)
    return (
        verifier,
        hashlib.sha256(verifier.encode("ascii")).hexdigest(),
        pysecrets.token_urlsafe(24),
    )


def authorization_url(client_key: str, redirect_uri: str, challenge: str, state: str) -> str:
    return (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_key": client_key,
                "scope": SCOPE,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk size, chunk count) under TikTok's rules: one chunk up to 64 MB; above that,
    10 MB chunks with the remainder folded into the last one."""
    if size <= SINGLE_CHUNK_MAX:
        return size, 1
    return CHUNK, size // CHUNK


class TikTokClient:
    def __init__(
        self,
        client_credentials: Callable[[], tuple[str, str] | None],
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client_credentials = client_credentials
        self._http = httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0), transport=transport)
        self._access: dict[str, tuple[str, float]] = {}
        # Called with (old refresh token, new refresh token) when TikTok rotates it.
        self.on_rotate: Callable[[str, str], None] | None = None

    def close(self) -> None:
        self._http.close()

    def _credentials(self) -> tuple[str, str]:
        creds = self._client_credentials()
        if not creds or not all(creds):
            raise PublishAuthError("the TikTok client key and secret are not set")
        return creds

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        key, secret = self._credentials()
        try:
            response = self._http.post(
                f"{API}/v2/oauth/token/", data={"client_key": key, "client_secret": secret, **data}
            )
        except httpx.HTTPError as exc:
            raise PublishError(f"could not reach TikTok ({type(exc).__name__})") from None
        try:
            body = response.json()
        except ValueError:
            raise PublishError("TikTok sign-in sent an unreadable reply") from None
        if response.status_code >= 500:
            raise PublishError(f"TikTok sign-in returned {response.status_code}")
        if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
            raise PublishAuthError("TikTok rejected the sign-in; connect the account again")
        return body

    def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> dict[str, Any]:
        body = self._token_request(
            {
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
        )
        if not body.get("refresh_token"):
            raise PublishAuthError("TikTok did not return a refresh token; try connecting again")
        return body

    def access_token(self, refresh_token: str) -> str:
        cached = self._access.get(refresh_token)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        body = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
        expires = time.monotonic() + max(60.0, float(body.get("expires_in") or 3600) - 300)
        self._access[refresh_token] = (body["access_token"], expires)
        new_refresh = body.get("refresh_token")
        if isinstance(new_refresh, str) and new_refresh and new_refresh != refresh_token:
            self._access[new_refresh] = self._access[refresh_token]
            if self.on_rotate is not None:
                self.on_rotate(refresh_token, new_refresh)
        return body["access_token"]

    def _api(self, refresh_token: str, path: str, payload: dict | None = None) -> dict:
        try:
            response = self._http.post(
                f"{API}{path}",
                json=payload or {},
                headers={"Authorization": f"Bearer {self.access_token(refresh_token)}"},
            )
        except httpx.HTTPError as exc:
            raise PublishError(f"could not reach TikTok ({type(exc).__name__})") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = error.get("code")
        if response.status_code == 401 or code in ("access_token_invalid", "scope_not_authorized"):
            self._access.pop(refresh_token, None)
            raise PublishAuthError("TikTok no longer accepts this account's sign-in")
        if response.status_code == 429 or code in (
            "rate_limit_exceeded",
            "spam_risk_too_many_pending_share",
        ):
            raise PublishError("TikTok's rate limit was reached")
        if response.status_code >= 500:
            raise PublishError(f"TikTok returned {response.status_code}")
        if response.status_code >= 400 or (code and code != "ok"):
            raise PublishRejected(
                f"TikTok refused the request ({code or response.status_code})", code
            )
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def creator_name(self, refresh_token: str) -> str | None:
        """The account's display name, if the app may read it; None otherwise."""
        try:
            response = self._http.get(
                f"{API}/v2/user/info/",
                params={"fields": "display_name"},
                headers={"Authorization": f"Bearer {self.access_token(refresh_token)}"},
            )
            name = response.json()["data"]["user"]["display_name"]
            return name if isinstance(name, str) and name else None
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None

    def start_inbox_upload(self, refresh_token: str, size: int) -> tuple[str, str]:
        chunk_size, chunks = chunk_plan(size)
        data = self._api(
            refresh_token,
            "/v2/post/publish/inbox/video/init/",
            {
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": chunks,
                }
            },
        )
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishError("TikTok did not return an upload address")
        return publish_id, upload_url

    def send_file(self, upload_url: str, path: Path) -> None:
        """PUT the file to the upload address, chunk by chunk, in order."""
        size = path.stat().st_size
        chunk_size, chunks = chunk_plan(size)
        with path.open("rb") as handle:
            for index in range(chunks):
                start = index * chunk_size
                end = size - 1 if index == chunks - 1 else start + chunk_size - 1
                handle.seek(start)
                data = handle.read(end - start + 1)
                try:
                    response = self._http.put(
                        upload_url,
                        content=data,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Range": f"bytes {start}-{end}/{size}",
                        },
                    )
                except httpx.HTTPError as exc:
                    raise PublishError(f"upload to TikTok failed ({type(exc).__name__})") from None
                if response.status_code not in (201, 206):
                    raise PublishError(f"TikTok's upload server returned {response.status_code}")

    def status(self, refresh_token: str, publish_id: str) -> tuple[str, str | None]:
        data = self._api(
            refresh_token, "/v2/post/publish/status/fetch/", {"publish_id": publish_id}
        )
        return str(data.get("status") or "UNKNOWN"), data.get("fail_reason")


class TikTokPublisher:
    """Delivers a clip to the account's TikTok inbox. One draft per job, even on retry."""

    platform = "tiktok"

    def __init__(self, client: TikTokClient, now: Callable[[], datetime]) -> None:
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
        publish_id = state.get("publish_id")
        if publish_id:
            status, reason = self.client.status(token, publish_id)
            if status in DELIVERED:
                return publish_id
            if state.get("uploaded"):
                if status == "FAILED":
                    raise PublishRejected(f"TikTok could not process the video ({reason})", reason)
                raise PublishPending("TikTok is still processing the upload")  # look again later
            started = datetime.fromisoformat(state["started_at"])
            if status != "FAILED" and self._now() - started < UPLOAD_URL_LIFETIME:
                # Same upload address, same draft: sending the file again cannot make two.
                self.client.send_file(state["upload_url"], video)
                save_state({"uploaded": True})
                raise PublishPending("TikTok is processing the upload")
        publish_id, upload_url = self.client.start_inbox_upload(token, video.stat().st_size)
        save_state(
            {
                "publish_id": publish_id,
                "upload_url": upload_url,
                "started_at": self._now().isoformat(),
                "uploaded": False,
            }
        )
        self.client.send_file(upload_url, video)
        save_state({"uploaded": True})
        status, _reason = self.client.status(token, publish_id)
        if status in DELIVERED:
            return publish_id
        raise PublishPending("TikTok is processing the upload")  # the next run confirms it

    def remove(self, token: str, post_id: str) -> bool:
        return False  # drafts and posts can only be deleted in the TikTok app

    def states(self, token: str, post_ids: list[str]) -> dict[str, dict]:
        return {}  # nothing to read back: what happens to a draft is up to its owner

    @staticmethod
    def url(post_id: str) -> str:
        return "In your TikTok inbox: finish and post it in the TikTok app"
