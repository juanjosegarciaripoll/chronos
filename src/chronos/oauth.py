"""OAuth 2.0 (loopback + device flow) + token storage + request signing.

This module bridges `chronos` to Google's OAuth 2.0 endpoints. The same
endpoints work for any Google CalDAV account; the `scope` on
`OAuthCredential` is the only knob users typically change.

Pieces:

- **Loopback flow** (`run_loopback_flow`, RFC 8252) — the default for
  desktop usage. We open the user's browser to Google's authorization
  page with `redirect_uri=http://127.0.0.1:<random-port>/`, listen on
  that port for the redirect, and exchange the auth code for tokens
  using PKCE. This is what "Desktop app" OAuth clients use (the same
  flow Thunderbird, gh CLI, vdirsyncer, etc. use). Requires a local
  browser.
- **Device flow** (`request_device_code`, `poll_for_tokens`, RFC 8628)
  — the SSH/headless fallback. The user opens a URL on any device,
  enters a short code, authorises chronos. No local browser needed,
  but Google requires the OAuth client be of type "TVs and Limited
  Input devices".
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

import base64
import contextlib
import hashlib
import http.server
import json
import os
import secrets
import stat
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import niquests
from niquests.auth import AuthBase

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_LOOPBACK_TIMEOUT_SECONDS = 300

_LOOPBACK_SUCCESS_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>chronos</title></head><body style='font-family:sans-serif;"
    "max-width:32em;margin:4em auto;text-align:center'>"
    "<h1>chronos: authorization received</h1>"
    "<p>You can close this window and return to your terminal.</p>"
    "</body></html>"
)

_LOOPBACK_ERROR_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>chronos: error</title></head><body style='font-family:sans-serif;"
    "max-width:32em;margin:4em auto;text-align:center'>"
    "<h1>chronos: authorization failed</h1>"
    "<p>{message}</p>"
    "<p>You can close this window and check the terminal.</p>"
    "</body></html>"
)


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


# Loopback flow (RFC 8252) ----------------------------------------------------


def _generate_pkce_pair() -> tuple[str, str]:
    """Generate (verifier, S256-challenge) for OAuth 2.0 PKCE.

    The verifier is a 64-byte URL-safe random string; the challenge is
    the base64url-encoded SHA-256 of the verifier (no padding), per RFC
    7636 §4.2. PKCE is required for Google "Desktop app" OAuth clients.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
    auth_url: str = _AUTH_URL,
) -> str:
    """Build the authorization-request URL the user's browser opens.

    `access_type=offline` + `prompt=consent` ensures Google returns a
    refresh token on every flow, even after the first time the user
    authorized (otherwise re-running the flow returns access_token only
    and our token store would lose the refresh_token).
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{auth_url}?{urllib.parse.urlencode(params)}"


def exchange_code_for_tokens(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    scope: str,
    http_post: Callable[..., Any] | None = None,
    now: Callable[[], float] | None = None,
) -> StoredTokens:
    """Exchange an authorization code (loopback flow) for tokens."""
    post = http_post or niquests.post
    clock = now or time.time
    response = cast(
        Any,
        post(
            _TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=30,
        ),
    )
    _raise_for_status(response, "token exchange")
    data = response.json()
    if not isinstance(data, dict):
        raise OAuthError(f"token exchange returned non-object JSON: {data!r}")
    payload = cast(dict[str, object], data)
    expires_in = _optional_int(payload, "expires_in", default=3600)
    return StoredTokens(
        access_token=_require_str(payload, "access_token"),
        refresh_token=_require_str(payload, "refresh_token"),
        expiry_unix=clock() + expires_in,
        scope=_optional_str(payload, "scope", default=scope),
    )


def _make_callback_handler(
    received: dict[str, str],
    done: threading.Event,
) -> type[http.server.BaseHTTPRequestHandler]:
    """HTTPRequestHandler class that captures the OAuth redirect query string.

    Closure-bound so each loopback flow has its own state — class
    attributes would race if two flows ever overlap.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "chronos-oauth/1"

        def do_GET(self) -> None:  # noqa: N802 — http.server callback name
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            for key, values in params.items():
                if values:
                    received[key] = values[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if "error" in received:
                body = _LOOPBACK_ERROR_HTML.format(message=received["error"])
            elif "code" in received:
                body = _LOOPBACK_SUCCESS_HTML
            else:
                body = _LOOPBACK_ERROR_HTML.format(
                    message="Missing 'code' and 'error' in callback"
                )
            self.wfile.write(body.encode("utf-8"))
            done.set()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ARG002
            # Silence the default per-request stderr logging — the
            # device-flow callback is internal plumbing, not server
            # traffic the user needs to see.
            pass

    return Handler


def run_loopback_flow(
    *,
    client_id: str,
    client_secret: str,
    scope: str,
    open_browser: Callable[[str], bool] | None = None,
    server_factory: Callable[
        [tuple[str, int], type[http.server.BaseHTTPRequestHandler]],
        http.server.HTTPServer,
    ]
    | None = None,
    timeout_seconds: float = _LOOPBACK_TIMEOUT_SECONDS,
    http_post: Callable[..., Any] | None = None,
    now: Callable[[], float] | None = None,
) -> StoredTokens:
    """OAuth 2.0 loopback flow with PKCE for Desktop-class clients.

    Listens on a random local port, opens the user's browser to
    Google's consent screen with that port as the redirect URI, waits
    for the redirect carrying the auth code (or error), and exchanges
    the code for tokens.

    Raises `OAuthError` if no browser is available, the redirect times
    out, the state token doesn't match (CSRF), or Google reports an
    error.
    """
    open_browser = open_browser or webbrowser.open
    make_server = server_factory or http.server.HTTPServer
    received: dict[str, str] = {}
    done = threading.Event()
    handler_cls = _make_callback_handler(received, done)
    server = make_server(("127.0.0.1", 0), handler_cls)
    try:
        port = server.server_port  # OS-assigned ephemeral port
        redirect_uri = f"http://127.0.0.1:{port}/"
        verifier, challenge = _generate_pkce_pair()
        state = secrets.token_urlsafe(16)
        auth_url = build_authorization_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            state=state,
            code_challenge=challenge,
        )
        if not open_browser(auth_url):
            raise OAuthError(
                "no browser available; cannot complete the loopback "
                "flow. Open the URL manually on a machine that can "
                "reach this one's localhost, or use the device flow "
                "(SSH / headless) instead.\n\n"
                f"{auth_url}\n"
            )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        if not done.wait(timeout=timeout_seconds):
            raise OAuthError(
                f"loopback flow timed out after {timeout_seconds:.0f}s; "
                "no callback received from the OAuth provider"
            )
        server.shutdown()
        thread.join(timeout=5)
    finally:
        server.server_close()

    if received.get("state") != state:
        raise OAuthError(
            "loopback flow: state token mismatch — refusing to "
            "complete (possible CSRF attempt)"
        )
    if "error" in received:
        description = received.get("error_description", "")
        raise OAuthError(
            f"loopback flow rejected: {received['error']}"
            + (f" ({description})" if description else "")
        )
    code = received.get("code")
    if not code:
        raise OAuthError(f"loopback flow: missing 'code' in callback: {received!r}")
    return exchange_code_for_tokens(
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        scope=scope,
        http_post=http_post,
        now=now,
    )


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


# Google account discovery ---------------------------------------------------


_GOOGLE_PRIMARY_CALENDAR_URL = (
    "https://www.googleapis.com/calendar/v3/calendars/primary"
)


def discover_google_user_email(
    bearer_auth: AuthBase,
    *,
    http_get: Callable[..., Any] | None = None,
) -> str:
    """Look up the authenticated user's email via Google's Calendar API.

    Returns the `id` field of the user's primary calendar, which Google
    sets to the user's email address. Used to build the per-user
    principal URL Google CalDAV's principal-discovery requires (a
    PROPFIND on the root URL `https://apidata.googleusercontent.com/
    caldav/v2/` returns 404; the per-user URL `<root>/<email>/user/`
    works).

    Reuses the existing Calendar OAuth scope, so no extra consent is
    needed beyond what `chronos sync` already asks for.
    """
    get = http_get or niquests.get
    response = cast(
        Any,
        get(_GOOGLE_PRIMARY_CALENDAR_URL, auth=bearer_auth, timeout=30),
    )
    _raise_for_status(response, "Google primary-calendar lookup")
    data = response.json()
    if not isinstance(data, dict):
        raise OAuthError(
            f"Google primary-calendar lookup returned non-object JSON: {data!r}"
        )
    return _require_str(cast(dict[str, object], data), "id")


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
    "build_authorization_url",
    "build_bearer_auth",
    "discover_google_user_email",
    "exchange_code_for_tokens",
    "load_tokens",
    "poll_for_tokens",
    "request_device_code",
    "run_loopback_flow",
    "save_tokens",
]
