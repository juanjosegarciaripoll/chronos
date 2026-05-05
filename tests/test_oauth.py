"""Unit tests for chronos.oauth.

All HTTP is mocked; none of these tests talk to Google.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock

from chronos.oauth import (
    BearerTokenAuth,
    OAuthError,
    StoredTokens,
    build_authorization_url,
    build_bearer_auth,
    exchange_code_for_tokens,
    load_tokens,
    refresh_access_token,
    run_loopback_flow,
    save_tokens,
)


class RefreshAccessTokenTest(unittest.TestCase):
    def test_returns_new_access_token_and_expiry(self) -> None:
        post = MagicMock(
            return_value={"access_token": "new-at", "expires_in": 3600}
        )
        token, expiry = refresh_access_token(
            client_id="c",
            client_secret="s",
            refresh_token="rt",
            http_post=post,
            now=lambda: 5000.0,
        )
        self.assertEqual(token, "new-at")
        self.assertEqual(expiry, 8600.0)


class TokenStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_round_trip(self) -> None:
        path = self.tmp / "tokens.json"
        tokens = StoredTokens(
            access_token="at",
            refresh_token="rt",
            expiry_unix=1234567890.0,
            scope="calendar",
        )
        save_tokens(path, tokens)
        self.assertEqual(load_tokens(path), tokens)

    def test_save_is_atomic_no_tempfile_leftovers(self) -> None:
        path = self.tmp / "tokens.json"
        save_tokens(
            path,
            StoredTokens(
                access_token="a", refresh_token="r", expiry_unix=0.0, scope="s"
            ),
        )
        leftovers = [p for p in self.tmp.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_load_missing_file_raises(self) -> None:
        with self.assertRaises(OAuthError) as ctx:
            load_tokens(self.tmp / "missing.json")
        self.assertIn("no stored tokens", str(ctx.exception))

    def test_save_writes_valid_json(self) -> None:
        path = self.tmp / "tokens.json"
        save_tokens(
            path,
            StoredTokens(
                access_token="a",
                refresh_token="r",
                expiry_unix=1.5,
                scope="s",
            ),
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            data,
            {
                "access_token": "a",
                "refresh_token": "r",
                "expiry_unix": 1.5,
                "scope": "s",
            },
        )

    def test_save_is_crash_safe_no_tmp_leftover(self) -> None:
        # Successful save must not leave a `.tmp-*` file behind.
        path = self.tmp / "tokens.json"
        save_tokens(
            path,
            StoredTokens(
                access_token="a",
                refresh_token="r",
                expiry_unix=1.5,
                scope="s",
            ),
        )
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_save_keyboard_interrupt_preserves_prior_file(self) -> None:
        path = self.tmp / "tokens.json"
        save_tokens(
            path,
            StoredTokens(
                access_token="original",
                refresh_token="original-rt",
                expiry_unix=1.0,
                scope="x",
            ),
        )
        with (
            unittest.mock.patch(
                "chronos.oauth.os.replace", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            save_tokens(
                path,
                StoredTokens(
                    access_token="overwriting",
                    refresh_token="overwriting-rt",
                    expiry_unix=2.0,
                    scope="x",
                ),
            )
        # Prior file untouched.
        loaded = load_tokens(path)
        self.assertEqual(loaded.access_token, "original")
        # No leftover tmp.
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])


class BearerTokenAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.token_path = self.tmp / "tokens.json"
        self.stored = StoredTokens(
            access_token="at-initial",
            refresh_token="rt",
            expiry_unix=1_000_000.0,
            scope="calendar",
        )

    def _auth(self, *, post: MagicMock, now_val: float = 0.0) -> BearerTokenAuth:
        return BearerTokenAuth(
            stored=self.stored,
            client_id="c",
            client_secret="s",
            scope="calendar",
            token_path=self.token_path,
            http_post=post,
            now=lambda: now_val,
        )

    def test_returns_bearer_header(self) -> None:
        auth = self._auth(post=MagicMock(), now_val=500_000.0)
        self.assertEqual(auth.get_header(), "Bearer at-initial")

    def test_refreshes_when_expired(self) -> None:
        post = MagicMock(
            return_value={"access_token": "at-refreshed", "expires_in": 3600}
        )
        # now > expiry_unix - skew → refresh triggered
        auth = self._auth(post=post, now_val=2_000_000.0)
        header = auth.get_header()
        self.assertEqual(header, "Bearer at-refreshed")
        self.assertTrue(auth.rotated)

    def test_persist_writes_rotated_tokens(self) -> None:
        post = MagicMock(
            return_value={"access_token": "at-refreshed", "expires_in": 100}
        )
        auth = self._auth(post=post, now_val=2_000_000.0)
        auth.get_header()
        auth.persist()
        self.assertTrue(self.token_path.exists())
        reloaded = load_tokens(self.token_path)
        self.assertEqual(reloaded.access_token, "at-refreshed")

    def test_persist_is_noop_when_not_rotated(self) -> None:
        auth = self._auth(post=MagicMock(), now_val=500_000.0)
        auth.get_header()
        auth.persist()
        self.assertFalse(self.token_path.exists())


class BuildBearerAuthTest(unittest.TestCase):
    def test_loads_stored_tokens(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        token_path = tmp / "tokens.json"
        save_tokens(
            token_path,
            StoredTokens(
                access_token="at",
                refresh_token="rt",
                expiry_unix=1e12,  # far future
                scope="calendar",
            ),
        )
        auth = build_bearer_auth(
            client_id="c",
            client_secret="s",
            scope="calendar",
            token_path=token_path,
        )
        self.assertIsInstance(auth, BearerTokenAuth)

    def test_missing_token_file_raises(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with self.assertRaises(OAuthError):
            build_bearer_auth(
                client_id="c",
                client_secret="s",
                scope="calendar",
                token_path=tmp / "not-there.json",
            )


class SaveTokensPermissionsTest(unittest.TestCase):
    def test_chmod_best_effort_posix(self) -> None:
        if os.name != "posix":
            self.skipTest("chmod semantics only meaningful on POSIX")
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = tmp / "tokens.json"
        save_tokens(
            path,
            StoredTokens(
                access_token="a",
                refresh_token="r",
                expiry_unix=0.0,
                scope="s",
            ),
        )
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class BuildAuthorizationUrlTest(unittest.TestCase):
    def test_url_contains_required_params(self) -> None:
        from urllib.parse import parse_qs, urlparse

        url = build_authorization_url(
            client_id="cid.apps.googleusercontent.com",
            redirect_uri="http://127.0.0.1:54321/",
            scope="https://www.googleapis.com/auth/calendar",
            state="csrf-state-token",
            code_challenge="abc123",
        )
        parsed = urlparse(url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.assertEqual(parsed.netloc, "accounts.google.com")
        self.assertEqual(params["client_id"], "cid.apps.googleusercontent.com")
        self.assertEqual(params["redirect_uri"], "http://127.0.0.1:54321/")
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["state"], "csrf-state-token")
        self.assertEqual(params["code_challenge"], "abc123")
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertEqual(params["access_type"], "offline")
        self.assertEqual(params["prompt"], "consent")


class ExchangeCodeForTokensTest(unittest.TestCase):
    def test_returns_stored_tokens_from_response(self) -> None:
        post = MagicMock(
            return_value={
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
                "scope": "https://example/scope",
            }
        )
        tokens = exchange_code_for_tokens(
            client_id="c",
            client_secret="s",
            code="auth-code-from-redirect",
            code_verifier="pkce-verifier",
            redirect_uri="http://127.0.0.1:1234/",
            scope="https://example/scope",
            http_post=post,
            now=lambda: 1000.0,
        )
        self.assertEqual(tokens.access_token, "at")
        self.assertEqual(tokens.refresh_token, "rt")
        self.assertEqual(tokens.expiry_unix, 4600.0)
        # The request carried the auth code and PKCE verifier
        (url, sent_data, timeout) = post.call_args.args
        self.assertEqual(sent_data["code"], "auth-code-from-redirect")
        self.assertEqual(sent_data["code_verifier"], "pkce-verifier")
        self.assertEqual(sent_data["grant_type"], "authorization_code")

    def test_http_error_raises(self) -> None:
        post = MagicMock(side_effect=OAuthError("HTTP 400 {'error': 'invalid_grant'}"))
        with self.assertRaises(OAuthError) as ctx:
            exchange_code_for_tokens(
                client_id="c",
                client_secret="s",
                code="x",
                code_verifier="v",
                redirect_uri="http://127.0.0.1:1/",
                scope="s",
                http_post=post,
            )
        self.assertIn("400", str(ctx.exception))


class LoopbackFlowTest(unittest.TestCase):
    """`run_loopback_flow` end-to-end: real `HTTPServer` on `127.0.0.1:0`,
    an in-test "browser" that fires the redirect via `urllib.request`,
    and a mocked token endpoint."""

    def _browser_that_calls_back(
        self,
        *,
        code: str = "auth-code-xyz",
        state_override: str | None = None,
    ) -> tuple[object, dict[str, str]]:
        import contextlib
        import threading
        import urllib.request
        from urllib.parse import parse_qs, urlparse

        captured: dict[str, str] = {}

        def open_browser(url: str) -> bool:
            captured["auth_url"] = url
            params = parse_qs(urlparse(url).query)
            redirect_uri = params["redirect_uri"][0]
            echo_state = (
                state_override if state_override is not None else params["state"][0]
            )
            callback = f"{redirect_uri}?code={code}&state={echo_state}"

            def fire() -> None:
                with contextlib.suppress(Exception):
                    urllib.request.urlopen(callback, timeout=5).read()  # noqa: S310

            threading.Thread(target=fire, daemon=True).start()
            return True

        return open_browser, captured

    def test_happy_path_exchanges_code(self) -> None:
        token_post = MagicMock(
            return_value={
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
                "scope": "https://example/scope",
            }
        )
        open_browser, captured = self._browser_that_calls_back(code="auth-code-xyz")
        tokens = run_loopback_flow(
            client_id="cid",
            client_secret="cs",
            scope="https://example/scope",
            open_browser=open_browser,  # type: ignore[arg-type]
            http_post=token_post,
            now=lambda: 5000.0,
            timeout_seconds=10,
        )
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A", captured["auth_url"])
        self.assertEqual(tokens.access_token, "at")
        self.assertEqual(tokens.refresh_token, "rt")
        self.assertEqual(tokens.expiry_unix, 8600.0)
        # Token endpoint received the code
        (url, sent_data, timeout) = token_post.call_args.args
        self.assertEqual(sent_data["code"], "auth-code-xyz")
        self.assertEqual(sent_data["grant_type"], "authorization_code")

    def test_no_browser_raises(self) -> None:
        with self.assertRaises(OAuthError) as ctx:
            run_loopback_flow(
                client_id="c",
                client_secret="s",
                scope="x",
                open_browser=lambda _url: False,
                timeout_seconds=1,
            )
        self.assertIn("no browser", str(ctx.exception))

    def test_state_mismatch_raises(self) -> None:
        open_browser, _ = self._browser_that_calls_back(state_override="FORGED-state")
        with self.assertRaises(OAuthError) as ctx:
            run_loopback_flow(
                client_id="c",
                client_secret="s",
                scope="x",
                open_browser=open_browser,  # type: ignore[arg-type]
                timeout_seconds=10,
            )
        self.assertIn("state", str(ctx.exception).lower())

    def test_timeout_when_no_callback(self) -> None:
        with self.assertRaises(OAuthError) as ctx:
            run_loopback_flow(
                client_id="c",
                client_secret="s",
                scope="x",
                open_browser=lambda _url: True,
                timeout_seconds=1,
            )
        self.assertIn("timed out", str(ctx.exception))

    def test_keyboard_interrupt_releases_listening_port(self) -> None:
        import http.server as _http_server
        import socket

        captured: dict[str, _http_server.HTTPServer] = {}

        def factory(
            address: tuple[str, int],
            handler_cls: type[_http_server.BaseHTTPRequestHandler],
        ) -> _http_server.HTTPServer:
            srv = _http_server.HTTPServer(address, handler_cls)
            captured["server"] = srv
            return srv

        def boom(_url: str) -> bool:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            run_loopback_flow(
                client_id="c",
                client_secret="s",
                scope="x",
                open_browser=boom,
                server_factory=factory,
                timeout_seconds=1,
            )

        srv = captured["server"]
        self.assertEqual(srv.socket.fileno(), -1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
