from __future__ import annotations

import os
import sys
import unittest

from chronos.credentials import (
    CredentialResolutionError,
    DefaultCredentialsProvider,
)
from chronos.domain import (
    CommandCredential,
    EnvCredential,
    KeyringCredential,
    PlaintextCredential,
)


class PlaintextBackendTest(unittest.TestCase):
    def test_returns_password_verbatim(self) -> None:
        provider = DefaultCredentialsProvider(env={})
        resolved = provider.resolve("acct", PlaintextCredential(password="s3cret"))
        self.assertEqual(resolved, "s3cret")


class EnvBackendTest(unittest.TestCase):
    def test_reads_env_variable(self) -> None:
        provider = DefaultCredentialsProvider(env={"MY_VAR": "from-env"})
        resolved = provider.resolve("acct", EnvCredential(variable="MY_VAR"))
        self.assertEqual(resolved, "from-env")

    def test_missing_var_raises(self) -> None:
        provider = DefaultCredentialsProvider(env={})
        with self.assertRaises(CredentialResolutionError) as ctx:
            provider.resolve("acct", EnvCredential(variable="MISSING"))
        self.assertIn("MISSING", str(ctx.exception))

    def test_empty_value_still_resolves(self) -> None:
        provider = DefaultCredentialsProvider(env={"EMPTY": ""})
        resolved = provider.resolve("acct", EnvCredential(variable="EMPTY"))
        self.assertEqual(resolved, "")


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
        resolved = provider.resolve("acct", self._echo("hunter2\n"))
        self.assertEqual(resolved, "hunter2")

    def test_missing_command_raises(self) -> None:
        provider = DefaultCredentialsProvider(env=os.environ)
        spec = CommandCredential(command=("this-binary-does-not-exist-xyz",))
        with self.assertRaises(CredentialResolutionError) as ctx:
            provider.resolve("acct", spec)
        self.assertIn("not found", str(ctx.exception))

    def test_nonzero_exit_raises(self) -> None:
        spec = CommandCredential(
            command=(sys.executable, "-c", "import sys; sys.exit(7)")
        )
        provider = DefaultCredentialsProvider(env=os.environ)
        with self.assertRaises(CredentialResolutionError) as ctx:
            provider.resolve("acct", spec)
        self.assertIn("exited 7", str(ctx.exception))


class KeyringBackendTest(unittest.TestCase):
    def test_keyring_raises_deferred_error(self) -> None:
        provider = DefaultCredentialsProvider(env={})
        spec = KeyringCredential(service="chronos", username="user@example.com")
        with self.assertRaises(CredentialResolutionError) as ctx:
            provider.resolve("acct", spec)
        message = str(ctx.exception)
        self.assertIn("keyring", message)
        self.assertIn("encrypted", message)
