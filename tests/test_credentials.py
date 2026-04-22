from __future__ import annotations

import os
import re
import sys
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
