"""Unit tests for chronos.oauth.

All HTTP is mocked; none of these tests talk to Google.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from chronos.oauth import (
    BearerTokenAuth,
    DeviceCodeGrant,
    OAuthError,
    StoredTokens,
    build_bearer_auth,
    load_tokens,
    poll_for_tokens,
    refresh_access_token,
    request_device_code,
    save_tokens,
)


def _mock_response(*, status: int = 200, json_body: dict[str, Any]) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.json.return_value = json_body
    return response


class RequestDeviceCodeTest(unittest.TestCase):
    def test_returns_grant_from_response(self) -> None:
        post = MagicMock(
            return_value=_mock_response(
                json_body={
                    "device_code": "dc-123",
                    "user_code": "ABCD-EFGH",
                    "verification_url": "https://www.google.com/device",
                    "interval": 5,
                    "expires_in": 1800,
                }
            )
        )
        grant = request_device_code(
            client_id="cid",
            scope="https://www.googleapis.com/auth/calendar",
            http_post=post,
        )
        self.assertEqual(grant.device_code, "dc-123")
        self.assertEqual(grant.user_code, "ABCD-EFGH")
        self.assertEqual(grant.verification_url, "https://www.google.com/device")
        self.assertEqual(grant.interval, 5)

    def test_falls_back_to_verification_uri_key(self) -> None:
        # Some Google endpoints use verification_uri instead of
        # verification_url.
        post = MagicMock(
            return_value=_mock_response(
                json_body={
                    "device_code": "dc",
                    "user_code": "xx",
                    "verification_uri": "https://example.com/device",
                    "interval": 1,
                    "expires_in": 30,
                }
            )
        )
        grant = request_device_code(client_id="c", scope="s", http_post=post)
        self.assertEqual(grant.verification_url, "https://example.com/device")

    def test_non_2xx_raises(self) -> None:
        post = MagicMock(
            return_value=_mock_response(
                status=400, json_body={"error": "invalid_client"}
            )
        )
        with self.assertRaises(OAuthError):
            request_device_code(client_id="c", scope="s", http_post=post)


class PollForTokensTest(unittest.TestCase):
    def _grant(self) -> DeviceCodeGrant:
        return DeviceCodeGrant(
            device_code="dc",
            user_code="uc",
            verification_url="u",
            interval=1,
            expires_in=30,
        )

    def test_returns_tokens_on_success(self) -> None:
        post = MagicMock(
            return_value=_mock_response(
                json_body={
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "expires_in": 3600,
                    "scope": "calendar",
                }
            )
        )
        tokens = poll_for_tokens(
            client_id="c",
            client_secret="s",
            grant=self._grant(),
            scope="calendar",
            http_post=post,
            sleep=lambda _n: None,
            now=lambda: 1000.0,
        )
        self.assertEqual(tokens.access_token, "at-1")
        self.assertEqual(tokens.refresh_token, "rt-1")
        self.assertEqual(tokens.expiry_unix, 4600.0)

    def test_retries_on_authorization_pending(self) -> None:
        post = MagicMock(
            side_effect=[
                _mock_response(
                    status=400,
                    json_body={"error": "authorization_pending"},
                ),
                _mock_response(
                    json_body={
                        "access_token": "at",
                        "refresh_token": "rt",
                        "expires_in": 60,
                    }
                ),
            ]
        )
        tokens = poll_for_tokens(
            client_id="c",
            client_secret="s",
            grant=self._grant(),
            scope="calendar",
            http_post=post,
            sleep=lambda _n: None,
            now=lambda: 0.0,
        )
        self.assertEqual(tokens.access_token, "at")
        self.assertEqual(post.call_count, 2)

    def test_raises_on_terminal_error(self) -> None:
        post = MagicMock(
            return_value=_mock_response(
                status=400,
                json_body={
                    "error": "access_denied",
                    "error_description": "user denied the request",
                },
            )
        )
        with self.assertRaises(OAuthError) as ctx:
            poll_for_tokens(
                client_id="c",
                client_secret="s",
                grant=self._grant(),
                scope="calendar",
                http_post=post,
                sleep=lambda _n: None,
                now=lambda: 0.0,
            )
        self.assertIn("access_denied", str(ctx.exception))

    def test_deadline_expiry_raises(self) -> None:
        post = MagicMock(
            return_value=_mock_response(
                status=400,
                json_body={"error": "authorization_pending"},
            )
        )
        # Fake clock advances past the deadline after the first call.
        times = iter([0.0, 31.0, 62.0])
        with self.assertRaises(OAuthError) as ctx:
            poll_for_tokens(
                client_id="c",
                client_secret="s",
                grant=self._grant(),
                scope="calendar",
                http_post=post,
                sleep=lambda _n: None,
                now=lambda: next(times),
            )
        self.assertIn("expired", str(ctx.exception))


class RefreshAccessTokenTest(unittest.TestCase):
    def test_returns_new_access_token_and_expiry(self) -> None:
        post = MagicMock(
            return_value=_mock_response(
                json_body={"access_token": "new-at", "expires_in": 3600}
            )
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

    def test_sets_authorization_header(self) -> None:
        auth = self._auth(post=MagicMock(), now_val=500_000.0)
        request = MagicMock()
        request.headers = {}
        auth(request)
        self.assertEqual(request.headers["Authorization"], "Bearer at-initial")

    def test_refreshes_when_expired(self) -> None:
        post = MagicMock(
            return_value=_mock_response(
                json_body={"access_token": "at-refreshed", "expires_in": 3600}
            )
        )
        # now > expiry_unix - skew → refresh triggered
        auth = self._auth(post=post, now_val=2_000_000.0)
        request = MagicMock()
        request.headers = {}
        auth(request)
        self.assertEqual(request.headers["Authorization"], "Bearer at-refreshed")
        self.assertTrue(auth.rotated)

    def test_persist_writes_rotated_tokens(self) -> None:
        post = MagicMock(
            return_value=_mock_response(
                json_body={"access_token": "at-refreshed", "expires_in": 100}
            )
        )
        auth = self._auth(post=post, now_val=2_000_000.0)
        request = MagicMock()
        request.headers = {}
        auth(request)
        auth.persist()
        self.assertTrue(self.token_path.exists())
        reloaded = load_tokens(self.token_path)
        self.assertEqual(reloaded.access_token, "at-refreshed")

    def test_persist_is_noop_when_not_rotated(self) -> None:
        auth = self._auth(post=MagicMock(), now_val=500_000.0)
        request = MagicMock()
        request.headers = {}
        auth(request)
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
