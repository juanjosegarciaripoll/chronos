"""OAuth 2.0 loopback flow + token storage + request signing.

This module bridges `chronos` to Google's OAuth 2.0 endpoints. The same
endpoints work for any Google CalDAV account; the `scope` on
`OAuthCredential` is the only knob users typically change.

Pieces:

- **Loopback flow** (`run_loopback_flow`, RFC 8252 + PKCE) — opens the
  user's browser to Google's authorization page with
  `redirect_uri=http://127.0.0.1:<random-port>/`, listens on that port
  for the redirect, and exchanges the auth code for tokens. This is
  what "Desktop app" OAuth clients use (same flow as Thunderbird, gh
  CLI, vdirsyncer, etc.). Requires a local browser.
- **Token store** (`save_tokens`, `load_tokens`) — plain JSON under
  `paths.oauth_token_dir()`, with a best-effort 0600 chmod (ignored
  on Windows). Contains the refresh token; keep it safe.
- **Bearer auth** (`BearerTokenAuth`) — checks the expiry and invokes
  the refresh grant (RFC 6749 §6) against `_TOKEN_URL` when needed.
  Call `get_header()` to get the current ``Authorization`` header value.

Implementation note: we deliberately do not depend on `google-auth`
or `niquests`/`requests`; all HTTP is done via `urllib.request`.
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
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
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
class StoredTokens:
    access_token: str
    refresh_token: str
    expiry_unix: float
    scope: str


# Default HTTP POST using stdlib urllib -----------------------------------


def _default_http_post(
    url: str, data: dict[str, str], timeout: float
) -> dict[str, object]:
    """POST form data to `url` and return parsed JSON dict."""
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            payload = json.loads(body.decode())
        except (ValueError, UnicodeDecodeError):
            payload = None
        raise OAuthError(f"HTTP {exc.code} {payload!r}") from exc
    except OSError as exc:
        raise OAuthError(f"network error: {exc}") from exc
    try:
        return cast(dict[str, object], json.loads(body.decode()))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OAuthError("invalid JSON response") from exc


# Loopback flow (RFC 8252 + PKCE) ---------------------------------------------


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
    http_post: Callable[[str, dict[str, str], float], dict[str, object]]
    | None = None,
    now: Callable[[], float] | None = None,
) -> StoredTokens:
    """Exchange an authorization code (loopback flow) for tokens."""
    post = http_post or _default_http_post
    clock = now or time.time
    data: dict[str, str] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    payload = post(_TOKEN_URL, data, 30.0)
    if not isinstance(payload, dict):
        raise OAuthError(f"token exchange returned non-object JSON: {payload!r}")
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
    """HTTPRequestHandler class that captures the OAuth redirect query string."""

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
    http_post: Callable[[str, dict[str, str], float], dict[str, object]]
    | None = None,
    now: Callable[[], float] | None = None,
) -> StoredTokens:
    """OAuth 2.0 loopback flow with PKCE for Desktop-class clients."""
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
                f"flow (listening on port {port}). Open the URL below "
                "on a machine that can reach this one — for example "
                f"via `ssh -L {port}:localhost:{port} <this-host>`.\n\n"
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


# Token storage ---------------------------------------------------------------


def save_tokens(path: Path, tokens: StoredTokens) -> None:
    """Atomic write of tokens to `path`. Sets 0600 perms on POSIX."""
    payload = json.dumps(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expiry_unix": tokens.expiry_unix,
            "scope": tokens.scope,
        },
        indent=2,
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    # Best-effort permission tightening on POSIX.
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
    http_post: Callable[[str, dict[str, str], float], dict[str, object]]
    | None = None,
    now: Callable[[], float] | None = None,
) -> tuple[str, float]:
    """Exchange a refresh token for a new access token.

    Returns `(access_token, expiry_unix)`.
    """
    post = http_post or _default_http_post
    clock = now or time.time
    data: dict[str, str] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    payload = post(_TOKEN_URL, data, 30.0)
    if not isinstance(payload, dict):
        raise OAuthError(f"token refresh returned non-object JSON: {payload!r}")
    access_token = _require_str(payload, "access_token")
    expires_in = _optional_int(payload, "expires_in", default=3600)
    return access_token, clock() + expires_in


class BearerTokenAuth:
    """Provides Bearer auth headers; refreshes expired access tokens.

    Holds the last known access/refresh tokens and the expiry. On every
    `get_header()` call, checks the expiry against `now()` (with a 60s
    skew margin) and invokes the refresh grant if needed. The caller can
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
        http_post: Callable[[str, dict[str, str], float], dict[str, object]]
        | None = None,
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

    def get_header(self) -> str:
        """Return 'Bearer <token>', refreshing if expired."""
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
        return f"Bearer {self._token}"

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


def _require_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
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
    "OAuthError",
    "StoredTokens",
    "build_authorization_url",
    "build_bearer_auth",
    "exchange_code_for_tokens",
    "load_tokens",
    "refresh_access_token",
    "run_loopback_flow",
    "save_tokens",
]
