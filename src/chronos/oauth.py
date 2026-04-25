"""OAuth 2.0 device flow + token storage + request signing.

This module bridges `chronos` to Google's OAuth 2.0 endpoints. The
same endpoints work for any Google CalDAV account; the `scope` on
`OAuthCredential` is the only knob users typically change.

Pieces:

- **Device flow** (`request_device_code`, `poll_for_tokens`) — the
  user opens a URL on any device, enters a short code, authorises
  chronos. No local webserver; works over SSH.
- **Token store** (`save_tokens`, `load_tokens`) — plain JSON under
  `paths.oauth_token_dir()`, with a best-effort 0600 chmod (ignored
  on Windows). Contains the refresh token; keep it safe.
- **Bearer auth** (`BearerTokenAuth`) — a `niquests.auth.AuthBase`
  that sets `Authorization: Bearer <access_token>` on every request
  and refreshes expired access tokens using the refresh grant (RFC
  6749 §6) against `_TOKEN_URL`.

Implementation note: we deliberately do not depend on `google-auth`
because its transport layer requires `requests`, which we don't ship.
The refresh-grant flow is ~40 lines of straightforward HTTP; cheaper
to maintain than pulling in a second HTTP library.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import niquests
from niquests.auth import AuthBase

_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True, kw_only=True)
class DeviceCodeGrant:
    device_code: str
    user_code: str
    verification_url: str
    interval: int
    expires_in: int


@dataclass(frozen=True, kw_only=True)
class StoredTokens:
    access_token: str
    refresh_token: str
    expiry_unix: float
    scope: str


# Device flow -----------------------------------------------------------------


def request_device_code(
    *,
    client_id: str,
    scope: str,
    http_post: Callable[..., Any] | None = None,
) -> DeviceCodeGrant:
    """Start the device-authorisation flow; returns a code for the user."""
    post = http_post or niquests.post
    response = cast(
        Any,
        post(
            _DEVICE_CODE_URL,
            data={"client_id": client_id, "scope": scope},
            timeout=30,
        ),
    )
    _raise_for_status(response, "device-code request")
    data = response.json()
    if not isinstance(data, dict):
        raise OAuthError(f"device-code request returned non-object JSON: {data!r}")
    payload = cast(dict[str, object], data)
    return DeviceCodeGrant(
        device_code=_require_str(payload, "device_code"),
        user_code=_require_str(payload, "user_code"),
        # Google sometimes uses verification_url, sometimes verification_uri.
        verification_url=_require_str(
            payload, "verification_url", fallback_key="verification_uri"
        ),
        interval=_optional_int(payload, "interval", default=5),
        expires_in=_optional_int(payload, "expires_in", default=1800),
    )


def poll_for_tokens(
    *,
    client_id: str,
    client_secret: str,
    grant: DeviceCodeGrant,
    scope: str,
    http_post: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> StoredTokens:
    """Poll the token endpoint until the user authorises or the grant expires."""
    post = http_post or niquests.post
    wait = sleep or time.sleep
    clock = now or time.time

    deadline = clock() + grant.expires_in
    interval = max(1, grant.interval)
    while clock() < deadline:
        wait(interval)
        response = cast(
            Any,
            post(
                _TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "device_code": grant.device_code,
                    "grant_type": ("urn:ietf:params:oauth:grant-type:device_code"),
                },
                timeout=30,
            ),
        )
        data = response.json()
        if not isinstance(data, dict):
            raise OAuthError(f"token request returned non-object JSON: {data!r}")
        payload = cast(dict[str, object], data)
        status = int(getattr(response, "status_code", 0))
        if status == 200:
            expires_in = _optional_int(payload, "expires_in", default=3600)
            return StoredTokens(
                access_token=_require_str(payload, "access_token"),
                refresh_token=_require_str(payload, "refresh_token"),
                expiry_unix=clock() + expires_in,
                scope=_optional_str(payload, "scope", default=scope),
            )
        error = _optional_str(payload, "error", default="")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        description = _optional_str(payload, "error_description", default="")
        raise OAuthError(
            f"device flow failed: {error or 'unknown'}"
            + (f": {description}" if description else "")
        )
    raise OAuthError("device flow expired; user did not authorise in time")


# Token storage ---------------------------------------------------------------


def save_tokens(path: Path, tokens: StoredTokens) -> None:
    """Atomic write of tokens to `path`. Sets 0600 perms on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expiry_unix": tokens.expiry_unix,
            "scope": tokens.scope,
        },
        indent=2,
    )
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    # Best-effort permission tightening on POSIX; chmod is a no-op on
    # Windows but harmless.
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def load_tokens(path: Path) -> StoredTokens:
    if not path.exists():
        raise OAuthError(
            f"no stored tokens at {path}. "
            "Run `chronos sync` from an interactive terminal to authorize."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise OAuthError(f"token file {path} is not a JSON object")
    data = cast(dict[str, object], raw)
    return StoredTokens(
        access_token=_require_str(data, "access_token"),
        refresh_token=_require_str(data, "refresh_token"),
        expiry_unix=_optional_float(data, "expiry_unix", default=0.0),
        scope=_optional_str(data, "scope", default=""),
    )


# Bearer-token HTTP auth ------------------------------------------------------


_EXPIRY_SKEW_SECONDS = 60


def refresh_access_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    http_post: Callable[..., Any] | None = None,
    now: Callable[[], float] | None = None,
) -> tuple[str, float]:
    """Exchange a refresh token for a new access token.

    Returns `(access_token, expiry_unix)`.
    """
    post = http_post or niquests.post
    clock = now or time.time
    response = cast(
        Any,
        post(
            _TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        ),
    )
    _raise_for_status(response, "token refresh")
    data = response.json()
    if not isinstance(data, dict):
        raise OAuthError(f"token refresh returned non-object JSON: {data!r}")
    payload = cast(dict[str, object], data)
    access_token = _require_str(payload, "access_token")
    expires_in = _optional_int(payload, "expires_in", default=3600)
    return access_token, clock() + expires_in


class BearerTokenAuth(AuthBase):
    """Sign HTTP requests with an access token; refresh on expiry.

    Holds the last known access/refresh tokens and the expiry. On every
    `__call__`, checks the expiry against `now()` (with a 60s skew
    margin) and invokes the refresh grant if needed. The caller can
    ask whether the token `rotated` and call `persist()` to write the
    new token back to disk.
    """

    def __init__(
        self,
        *,
        stored: StoredTokens,
        client_id: str,
        client_secret: str,
        scope: str,
        token_path: Path,
        http_post: Callable[..., Any] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._token = stored.access_token
        self._refresh_token = stored.refresh_token
        self._expiry_unix = stored.expiry_unix
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._token_path = token_path
        self._http_post = http_post
        self._now = now or time.time
        self._initial_token = stored.access_token

    def __call__(self, request: Any) -> Any:
        if self._now() >= self._expiry_unix - _EXPIRY_SKEW_SECONDS:
            new_token, new_expiry = refresh_access_token(
                client_id=self._client_id,
                client_secret=self._client_secret,
                refresh_token=self._refresh_token,
                http_post=self._http_post,
                now=self._now,
            )
            self._token = new_token
            self._expiry_unix = new_expiry
        request.headers["Authorization"] = f"Bearer {self._token}"
        return request

    @property
    def rotated(self) -> bool:
        return self._token != self._initial_token

    def persist(self) -> None:
        if not self.rotated:
            return
        save_tokens(
            self._token_path,
            StoredTokens(
                access_token=self._token,
                refresh_token=self._refresh_token,
                expiry_unix=self._expiry_unix,
                scope=self._scope,
            ),
        )
        self._initial_token = self._token


def build_bearer_auth(
    *,
    client_id: str,
    client_secret: str,
    scope: str,
    token_path: Path,
) -> BearerTokenAuth:
    """Load stored tokens for an account and wrap them in BearerTokenAuth."""
    stored = load_tokens(token_path)
    return BearerTokenAuth(
        stored=stored,
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
        token_path=token_path,
    )


# Helpers ---------------------------------------------------------------------


def _raise_for_status(response: Any, label: str) -> None:
    status = int(getattr(response, "status_code", 0))
    if 200 <= status < 300:
        return
    try:
        body = response.json()
    except (ValueError, niquests.exceptions.RequestException):
        body = None
    raise OAuthError(f"{label} failed: HTTP {status} {body!r}")


def _require_str(
    data: dict[str, object], key: str, *, fallback_key: str | None = None
) -> str:
    value = data.get(key)
    if value is None and fallback_key is not None:
        value = data.get(fallback_key)
    if not isinstance(value, str) or not value:
        raise OAuthError(f"missing/invalid {key!r} in OAuth payload: {data!r}")
    return value


def _optional_str(data: dict[str, object], key: str, *, default: str) -> str:
    value = data.get(key)
    if isinstance(value, str):
        return value
    return default


def _optional_int(data: dict[str, object], key: str, *, default: int) -> int:
    value = data.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _optional_float(data: dict[str, object], key: str, *, default: float) -> float:
    value = data.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


__all__ = [
    "BearerTokenAuth",
    "DeviceCodeGrant",
    "OAuthError",
    "StoredTokens",
    "build_bearer_auth",
    "load_tokens",
    "poll_for_tokens",
    "request_device_code",
    "save_tokens",
]
