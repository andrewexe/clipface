"""Instagram Reels through the Graph API, with a resumable upload from this computer.

Needs: an Instagram professional account linked to a Facebook Page, a Meta app, and a
user access token from Facebook Login for Business with `instagram_content_publish`.
Meta only allows uploading a local file (the resumable upload) with that kind of token;
other tokens need the video on a public web server, which a local app does not have.

Flow (graph.facebook.com, version below):
  POST /<ig-id>/media?media_type=REELS&upload_type=resumable   -> container id
  POST rupload.facebook.com/ig-api-upload/<version>/<container>  the file
  GET  /<container>?fields=status_code                           until FINISHED
  POST /<ig-id>/media_publish?creation_id=<container>            -> media id
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from engine.publish.errors import (
    PublishAuthError,
    PublishError,
    PublishPending,
    PublishRejected,
)

log = logging.getLogger(__name__)

VERSION = "v27.0"
GRAPH = f"https://graph.facebook.com/{VERSION}"
UPLOAD = f"https://rupload.facebook.com/ig-api-upload/{VERSION}"
CAPTION_MAX = 2200
CLOCK_SKEW = timedelta(minutes=15)


class InstagramClient:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(timeout=httpx.Timeout(180.0, connect=15.0), transport=transport)

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, url: str, token: str, **kwargs: Any) -> dict:
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        try:
            response = self._http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise PublishError(f"could not reach Instagram ({type(exc).__name__})") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code < 300 and isinstance(body, dict) and "error" not in body:
            return body
        error = (
            body.get("error")
            if isinstance(body, dict) and isinstance(body.get("error"), dict)
            else {}
        )
        code = error.get("code")
        if response.status_code == 401 or code == 190:
            raise PublishAuthError("Instagram no longer accepts this access token")
        if response.status_code == 429 or code in (4, 17, 32, 613) or error.get("is_transient"):
            raise PublishError("Instagram's rate limit was reached")
        if response.status_code >= 500:
            raise PublishError(f"Instagram returned {response.status_code}")
        message = str(error.get("message") or response.status_code)[:160]
        raise PublishRejected(f"Instagram refused the request ({message})", str(code))

    def account(self, token: str) -> dict[str, str]:
        """The Instagram professional account this token can publish to."""
        body = self._call(
            "GET", f"{GRAPH}/me/accounts", token,
            params={"fields": "instagram_business_account{id,username}"},
        )  # fmt: skip
        for page in body.get("data", []):
            account = page.get("instagram_business_account") if isinstance(page, dict) else None
            if isinstance(account, dict) and account.get("id"):
                return {
                    "id": str(account["id"]),
                    "username": str(account.get("username") or "instagram"),
                }
        raise PublishRejected(
            "this token has no Instagram professional account linked to a Facebook Page"
        )

    def create_container(self, token: str, ig_id: str, caption: str) -> str:
        body = self._call(
            "POST", f"{GRAPH}/{ig_id}/media", token,
            json={
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption[:CAPTION_MAX],
            },
        )  # fmt: skip
        if not body.get("id"):
            raise PublishError("Instagram did not return a container")
        return str(body["id"])

    def upload(self, token: str, container_id: str, path: Path) -> None:
        size = path.stat().st_size
        try:
            response = self._http.post(
                f"{UPLOAD}/{container_id}",
                content=path.read_bytes(),
                headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
            )
            ok = response.status_code < 300 and response.json().get("success") is True
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise PublishError(f"upload to Instagram failed ({type(exc).__name__})") from None
        if not ok:
            raise PublishError("Instagram did not accept the upload")

    def container_status(self, token: str, container_id: str) -> str:
        body = self._call("GET", f"{GRAPH}/{container_id}", token, params={"fields": "status_code"})
        return str(body.get("status_code") or "UNKNOWN")

    def publish_container(self, token: str, ig_id: str, container_id: str) -> str:
        body = self._call(
            "POST", f"{GRAPH}/{ig_id}/media_publish", token, json={"creation_id": container_id}
        )
        if not body.get("id"):
            raise PublishError("Instagram published but returned no media id")
        return str(body["id"])

    def recent_media(self, token: str, ig_id: str) -> list[dict]:
        body = self._call(
            "GET", f"{GRAPH}/{ig_id}/media", token,
            params={"fields": "id,caption,timestamp,permalink", "limit": "10"},
        )  # fmt: skip
        return [item for item in body.get("data", []) if isinstance(item, dict)]

    def insights(self, token: str, media_id: str) -> dict | None:
        try:
            body = self._call(
                "GET", f"{GRAPH}/{media_id}", token,
                params={"fields": "like_count,comments_count,permalink"},
            )  # fmt: skip
        except PublishRejected:
            return None  # the media is gone
        return {
            "upload_status": "processed",
            "views": 0,
            "likes": int(body.get("like_count") or 0),
            "comments": int(body.get("comments_count") or 0),
        }


class InstagramPublisher:
    platform = "instagram"

    def __init__(self, client: InstagramClient, now: Callable[[], datetime]) -> None:
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
        caption = f"{title}\n\n{description}"[:CAPTION_MAX]
        ig_id = state.get("ig_id") or self.client.account(token)["id"]
        container = state.get("container_id")
        if container:
            status = self.client.container_status(token, container)
            if status == "PUBLISHED":
                # Published, but we never got the media id: find it by caption and time.
                started = datetime.fromisoformat(state["started_at"]) - CLOCK_SKEW
                for item in self.client.recent_media(token, ig_id):
                    try:
                        posted = datetime.fromisoformat(
                            item["timestamp"].replace("+0000", "+00:00")
                        )
                    except (KeyError, ValueError, AttributeError):
                        continue
                    if item.get("caption") == caption and posted >= started:
                        return str(item["id"])
                raise PublishPending("Instagram says it is published; waiting for it to appear")
            if status == "FINISHED":
                return self.client.publish_container(token, ig_id, container)
            if status == "IN_PROGRESS":
                if not state.get("uploaded"):
                    self.client.upload(token, container, video)
                    save_state({"uploaded": True})
                raise PublishPending("Instagram is still processing the video")
            # ERROR or EXPIRED: that container is dead, and nothing was published from it.
        container = self.client.create_container(token, ig_id, caption)
        save_state(
            {
                "ig_id": ig_id,
                "container_id": container,
                "started_at": self._now().isoformat(),
                "uploaded": False,
            }
        )
        self.client.upload(token, container, video)
        save_state({"uploaded": True})
        if self.client.container_status(token, container) == "FINISHED":
            return self.client.publish_container(token, ig_id, container)
        raise PublishPending("Instagram is processing the video")  # the next run publishes it

    def remove(self, token: str, post_id: str) -> bool:
        return False  # the Graph API cannot delete a published reel

    def states(self, token: str, post_ids: list[str]) -> dict[str, dict]:
        states = {}
        for post_id in post_ids:
            seen = self.client.insights(token, post_id)
            if seen is not None:
                states[post_id] = seen
        return states

    @staticmethod
    def url(post_id: str) -> str:
        return f"Instagram media {post_id}"
