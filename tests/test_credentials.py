from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

from chronos.credentials import (
    CredentialResolutionError,
    DefaultCredentialsProvider,
)
from chronos.domain import (
    AccountConfig,
    CommandCredential,
    CredentialSpec,
    EnvCredential,
    KeyringCredential,
    PlaintextCredential,
)


def _account(
    credential: CredentialSpec, *, username: str = "user@example.com"
) -> AccountConfig:
    return AccountConfig(
        name="acct",
        url="https://caldav.example.com/dav/",
        username=username,
        credential=credential,
        mirror_path=Path("/unused"),
        trash_retention_days=30,
        include=(re.compile(".*"),),
        exclude=(),
        read_only=(),
    )


def _resolve_password(
    provider: DefaultCredentialsProvider, credential: CredentialSpec
) -> str:
    auth = provider.build_auth(_account(credential))
    assert auth.basic is not None, "basic auth expected"
    return auth.basic[1]


class PlaintextBackendTest(unittest.TestCase):
    def test_returns_password_verbatim(self) -> None:
        provider = DefaultCredentialsProvider(env={})
        self.assertEqual(
            _resolve_password(provider, PlaintextCredential(password="s3cret")),
            "s3cret",
        )


class EnvBackendTest(unittest.TestCase):
    def test_reads_env_variable(self) -> None:
        provider = DefaultCredentialsProvider(env={"MY_VAR": "from-env"})
        self.assertEqual(
            _resolve_password(provider, EnvCredential(variable="MY_VAR")),
            "from-env",
        )

    def test_missing_var_raises(self) -> None:
        provider = DefaultCredentialsProvider(env={})
        with self.assertRaises(CredentialResolutionError) as ctx:
            _resolve_password(provider, EnvCredential(variable="MISSING"))
        self.assertIn("MISSING", str(ctx.exception))

    def test_empty_value_still_resolves(self) -> None:
        provider = DefaultCredentialsProvider(env={"EMPTY": ""})
        self.assertEqual(
            _resolve_password(provider, EnvCredential(variable="EMPTY")),
            "",
        )


class CommandBackendTest(unittest.TestCase):
    def _echo(self, text: str) -> CommandCredential:
        # Use the current Python interpreter to keep the test
        # cross-platform (no assumptions about /bin/echo).
        return CommandCredential(
            command=(
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write({text!r})",
            )
        )

    def test_captures_stdout_and_strips_trailing_newline(self) -> None:
        provider = DefaultCredentialsProvider(env=os.environ)
        self.assertEqual(
            _resolve_password(provider, self._echo("hunter2\n")), "hunter2"
        )

    def test_missing_command_raises(self) -> None:
        provider = DefaultCredentialsProvider(env=os.environ)
        spec = CommandCredential(command=("this-binary-does-not-exist-xyz",))
        with self.assertRaises(CredentialResolutionError) as ctx:
            _resolve_password(provider, spec)
        self.assertIn("not found", str(ctx.exception))

    def test_nonzero_exit_raises(self) -> None:
        spec = CommandCredential(
            command=(sys.executable, "-c", "import sys; sys.exit(7)")
        )
        provider = DefaultCredentialsProvider(env=os.environ)
        with self.assertRaises(CredentialResolutionError) as ctx:
            _resolve_password(provider, spec)
        self.assertIn("exited 7", str(ctx.exception))


class KeyringBackendTest(unittest.TestCase):
    def test_keyring_raises_deferred_error(self) -> None:
        provider = DefaultCredentialsProvider(env={})
        spec = KeyringCredential(service="chronos", username="user@example.com")
        with self.assertRaises(CredentialResolutionError) as ctx:
            _resolve_password(provider, spec)
        message = str(ctx.exception)
        self.assertIn("keyring", message)
        self.assertIn("encrypted", message)


class BuildAuthTest(unittest.TestCase):
    def test_builds_basic_auth_with_account_username(self) -> None:
        provider = DefaultCredentialsProvider(env={"PW": "secret"})
        account = _account(EnvCredential(variable="PW"), username="alice@example.com")
        auth = provider.build_auth(account)
        self.assertEqual(auth.basic, ("alice@example.com", "secret"))
        self.assertIsNone(auth.http_auth)
        self.assertIsNone(auth.on_commit)


class GoogleBackendTest(unittest.TestCase):
    """`GoogleCredential` is OAuth-with-defaults; verify it routes to OAuth
    and uses the fixed Google scope rather than the basic-auth code path.
    """

    def test_google_routes_through_oauth_with_google_scope(self) -> None:
        from unittest.mock import MagicMock, patch

        from chronos.domain import GOOGLE_OAUTH_SCOPE, GoogleCredential

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        token_path = tmp / "tokens.json"
        # Presence is enough; build_bearer_auth is mocked below.
        token_path.write_text("{}", encoding="utf-8")

        provider = DefaultCredentialsProvider(env={})
        account = _account(
            GoogleCredential(client_id="cid", client_secret="cs"), username=""
        )
        bearer = MagicMock()
        bearer.persist = MagicMock()
        with (
            patch(
                "chronos.credentials.build_bearer_auth", return_value=bearer
            ) as mock_build,
            patch(
                "chronos.credentials.oauth_token_path",
                return_value=token_path,
            ),
        ):
            auth = provider.build_auth(account)
        mock_build.assert_called_once_with(
            client_id="cid",
            client_secret="cs",
            scope=GOOGLE_OAUTH_SCOPE,
            token_path=token_path,
        )
        self.assertIsNone(auth.basic)
        self.assertIs(auth.http_auth, bearer)
        self.assertIs(auth.on_commit, bearer.persist)


class InteractiveAuthorizerTest(unittest.TestCase):
    """Missing OAuth tokens trigger the configured authorizer; without one,
    the provider raises a clean error pointing at the CLI sync path."""

    def test_missing_tokens_calls_authorizer_and_persists(self) -> None:
        from unittest.mock import MagicMock, patch

        from chronos.domain import OAuthCredential
        from chronos.oauth import StoredTokens

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        token_path = tmp / "tokens.json"
        self.assertFalse(token_path.exists())

        captured: dict[str, object] = {}
        fresh_tokens = StoredTokens(
            access_token="at",
            refresh_token="rt",
            expiry_unix=1e12,
            scope="https://example/scope",
        )

        def authorizer(
            account_name: str, spec: OAuthCredential, path: Path
        ) -> StoredTokens:
            captured["account"] = account_name
            captured["spec"] = spec
            captured["path"] = path
            return fresh_tokens

        provider = DefaultCredentialsProvider(env={}, interactive_authorizer=authorizer)
        account = _account(
            OAuthCredential(
                client_id="cid",
                client_secret="cs",
                scope="https://example/scope",
                token_path=token_path,
            ),
        )
        bearer = MagicMock()
        bearer.persist = MagicMock()
        with patch(
            "chronos.credentials.build_bearer_auth", return_value=bearer
        ) as mock_build:
            auth = provider.build_auth(account)

        # The authorizer was called with the account context.
        self.assertEqual(captured["account"], "acct")
        self.assertEqual(captured["path"], token_path)
        # And the returned tokens were persisted to the expected path.
        self.assertTrue(token_path.exists())
        # build_bearer_auth runs after persistence so it sees real tokens.
        mock_build.assert_called_once()
        self.assertIs(auth.http_auth, bearer)

    def test_missing_tokens_without_authorizer_raises_clean_error(self) -> None:
        from chronos.domain import OAuthCredential

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        token_path = tmp / "absent.json"

        provider = DefaultCredentialsProvider(env={})  # no authorizer
        account = _account(
            OAuthCredential(
                client_id="c",
                client_secret="s",
                scope="x",
                token_path=token_path,
            ),
        )
        with self.assertRaises(CredentialResolutionError) as ctx:
            provider.build_auth(account)
        message = str(ctx.exception)
        self.assertIn("no stored OAuth tokens", message)
        self.assertIn("chronos sync", message)

    def test_authorizer_failure_surfaces_as_credential_error(self) -> None:
        from chronos.domain import OAuthCredential
        from chronos.oauth import OAuthError, StoredTokens

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        token_path = tmp / "absent.json"

        def failing_authorizer(
            _name: str, _spec: OAuthCredential, _path: Path
        ) -> StoredTokens:
            raise OAuthError("user declined authorization")

        provider = DefaultCredentialsProvider(
            env={}, interactive_authorizer=failing_authorizer
        )
        account = _account(
            OAuthCredential(
                client_id="c",
                client_secret="s",
                scope="x",
                token_path=token_path,
            ),
        )
        with self.assertRaises(CredentialResolutionError) as ctx:
            provider.build_auth(account)
        self.assertIn("user declined", str(ctx.exception))
        # The empty-handed authorizer must NOT have left a token file
        # behind: persistence only happens on success.
        self.assertFalse(token_path.exists())
