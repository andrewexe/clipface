"""Official Twitch and Kick API clients for live status.

Both use an app access token (OAuth client credentials). Shapes follow the current docs:
- Twitch: POST id.twitch.tv/oauth2/token; GET api.twitch.tv/helix/streams?user_login=
  (max 100, offline channels are omitted); GET helix/users?login= (max 100).
- Kick: POST id.kick.com/oauth/token; GET api.kick.com/public/v1/channels?slug=
  (max 50, each at most 25 characters); GET public/v1/users?id=.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

Credentials = tuple[str, str]  # (client id, client secret)
USER_AGENT = "clipper-engine"
TIMEOUT = httpx.Timeout(10.0)
_AUTH_RETRY_AFTER_S = 300  # don't hammer the token endpoint with credentials that failed


class PlatformError(Exception):
    """A temporary failure: network trouble, a 5xx, or a rate limit."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class MissingCredentials(PlatformError):
    """The user has not entered this platform's keys yet."""


class PlatformAuthError(PlatformError):
    """The platform rejected the client ID or secret."""


@dataclass(frozen=True)
class LiveInfo:
    started_at: datetime | None
    title: str | None
    game: str | None
    viewer_count: int | None = None
    stream_id: str | None = None


@dataclass
class Snapshot:
    """Result of one check. `live` holds only channels that are live right now."""

    live: dict[str, LiveInfo] = field(default_factory=dict)
    # Among the names we were asked to verify: display names of the ones that exist...
    found: dict[str, str] = field(default_factory=dict)
    # ...and the ones the platform does not know.
    missing: set[str] = field(default_factory=set)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.year < 2000:  # Kick sends a zero time for offline channels
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _chunks(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class _OAuthClient:
    """Shared app-token handling: fetch, cache, refresh on 401, back off on bad keys."""

    platform: str
    token_url: str

    def __init__(
        self,
        credentials: Callable[[], Credentials | None],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._http = httpx.AsyncClient(
            timeout=TIMEOUT, transport=transport, headers={"User-Agent": USER_AGENT}
        )
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_creds: Credentials | None = None
        self._rejected: tuple[Credentials, float] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _current_credentials(self) -> Credentials:
        # keyring calls block; keep them off the event loop.
        creds = await asyncio.to_thread(self._credentials)
        if not creds or not all(creds):
            raise MissingCredentials(f"{self.platform} keys are not set")
        return creds

    async def _get_token(self, creds: Credentials) -> str:
        if self._token and self._token_creds == creds and time.monotonic() < self._token_expires:
            return self._token
        if self._rejected and self._rejected[0] == creds and time.monotonic() < self._rejected[1]:
            raise PlatformAuthError(f"{self.platform} rejected the client ID or secret")
        try:
            response = await self._http.post(
                self.token_url,
                data={
                    "client_id": creds[0],
                    "client_secret": creds[1],
                    "grant_type": "client_credentials",
                },
            )
        except httpx.HTTPError as exc:
            raise PlatformError(f"{self.platform} sign-in failed: {type(exc).__name__}") from None
        if response.status_code in (400, 401, 403):
            self._rejected = (creds, time.monotonic() + _AUTH_RETRY_AFTER_S)
            raise PlatformAuthError(f"{self.platform} rejected the client ID or secret")
        if response.status_code != 200:
            raise PlatformError(f"{self.platform} sign-in returned {response.status_code}")
        try:
            body = response.json()
            token = body["access_token"]
            expires_in = float(body.get("expires_in") or 3600)
        except (ValueError, KeyError, TypeError):
            raise PlatformError(f"{self.platform} sign-in returned an unreadable reply") from None
        self._rejected = None
        self._token, self._token_creds = token, creds
        self._token_expires = time.monotonic() + max(60.0, expires_in - 300)
        return token

    def _headers(self, creds: Credentials, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def _get(self, url: str, params: list[tuple[str, str]]) -> Any:
        creds = await self._current_credentials()
        for attempt in (1, 2):
            token = await self._get_token(creds)
            try:
                response = await self._http.get(
                    url, params=params, headers=self._headers(creds, token)
                )
            except httpx.HTTPError as exc:
                raise PlatformError(
                    f"{self.platform} request failed: {type(exc).__name__}"
                ) from None
            if response.status_code == 401 and attempt == 1:
                self._token = None  # expired or revoked: get a fresh one and retry once
                continue
            if response.status_code == 401:
                raise PlatformAuthError(f"{self.platform} rejected the access token")
            if response.status_code == 429:
                raise PlatformError(
                    f"{self.platform} rate limit reached", self._retry_after(response)
                )
            if response.status_code != 200:
                raise PlatformError(f"{self.platform} returned {response.status_code}")
            try:
                return response.json()
            except ValueError:
                raise PlatformError(f"{self.platform} returned an unreadable reply") from None
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        reset = response.headers.get("Ratelimit-Reset")  # Twitch: unix time of bucket refill
        if reset and reset.isdigit():
            return min(300.0, max(1.0, float(reset) - time.time()))
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return min(300.0, max(1.0, float(retry_after)))
        return 30.0


def _rows(body: Any) -> list[dict[str, Any]]:
    data = body.get("data") if isinstance(body, dict) else None
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


class TwitchClient(_OAuthClient):
    platform = "twitch"
    token_url = "https://id.twitch.tv/oauth2/token"
    api = "https://api.twitch.tv/helix"

    def _headers(self, creds: Credentials, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Client-Id": creds[0]}

    async def check(self, usernames: Sequence[str], verify: Collection[str]) -> Snapshot:
        snapshot = Snapshot()
        for batch in _chunks(list(usernames), 100):
            body = await self._get(f"{self.api}/streams", [("user_login", n) for n in batch])
            for row in _rows(body):
                login = str(row.get("user_login", "")).lower()
                if login and row.get("type", "live") == "live":
                    snapshot.live[login] = LiveInfo(
                        started_at=parse_time(row.get("started_at")),
                        title=_text(row.get("title")),
                        game=_text(row.get("game_name")),
                        viewer_count=row.get("viewer_count")
                        if isinstance(row.get("viewer_count"), int)
                        else None,
                        stream_id=_text(row.get("id")),
                    )
        to_verify = sorted(set(verify) & set(usernames))
        for batch in _chunks(to_verify, 100):
            body = await self._get(f"{self.api}/users", [("login", n) for n in batch])
            for row in _rows(body):
                login = str(row.get("login", "")).lower()
                if login:
                    snapshot.found[login] = _text(row.get("display_name")) or login
        snapshot.missing = set(to_verify) - set(snapshot.found)
        return snapshot


class KickClient(_OAuthClient):
    platform = "kick"
    token_url = "https://id.kick.com/oauth/token"
    api = "https://api.kick.com/public/v1"

    async def check(self, usernames: Sequence[str], verify: Collection[str]) -> Snapshot:
        snapshot = Snapshot()
        wanted = set(verify) & set(usernames)
        seen: set[str] = set()
        user_ids: dict[int, str] = {}
        for batch in _chunks(list(usernames), 50):
            body = await self._get(f"{self.api}/channels", [("slug", n) for n in batch])
            for row in _rows(body):
                slug = str(row.get("slug", "")).lower()
                if not slug:
                    continue
                seen.add(slug)
                if slug in wanted and isinstance(row.get("broadcaster_user_id"), int):
                    user_ids[row["broadcaster_user_id"]] = slug
                stream = row.get("stream") if isinstance(row.get("stream"), dict) else {}
                if stream.get("is_live") is True:
                    category = row.get("category") if isinstance(row.get("category"), dict) else {}
                    snapshot.live[slug] = LiveInfo(
                        started_at=parse_time(stream.get("start_time")),
                        title=_text(row.get("stream_title")),
                        game=_text(category.get("name")),
                        viewer_count=stream.get("viewer_count")
                        if isinstance(stream.get("viewer_count"), int)
                        else None,
                    )
        snapshot.found = {slug: slug for slug in wanted & seen}
        snapshot.missing = wanted - seen
        # The channel object has no display name; the users endpoint does.
        for batch in _chunks([str(i) for i in user_ids], 50):
            try:
                body = await self._get(f"{self.api}/users", [("id", i) for i in batch])
            except PlatformError as exc:
                log.info("kick display-name lookup skipped: %s", exc)
                break
            for row in _rows(body):
                slug = user_ids.get(row.get("user_id"))
                if slug and _text(row.get("name")):
                    snapshot.found[slug] = row["name"].strip()
        return snapshot
