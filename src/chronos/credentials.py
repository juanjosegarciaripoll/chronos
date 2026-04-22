from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping

from chronos.authorization import Authorization
from chronos.domain import (
    AccountConfig,
    CommandCredential,
    CredentialSpec,
    EnvCredential,
    PlaintextCredential,
)

_COMMAND_TIMEOUT_SECONDS = 30


class CredentialResolutionError(RuntimeError):
    pass


class DefaultCredentialsProvider:
    """Build an `Authorization` for one account from its credential spec.

    Backends:
      - PlaintextCredential: returns the stored string as a basic-auth
        password.
      - EnvCredential: reads the named environment variable.
      - CommandCredential: runs the command and uses stdout (stripped)
        as the basic-auth password.
      - KeyringCredential: deferred in v1 (requires the `keyring`
        package).
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = env if env is not None else os.environ

    def build_auth(self, account: AccountConfig) -> Authorization:
        password = self._resolve_password(account.name, account.credential)
        return Authorization(basic=(account.username, password))

    def _resolve_password(self, account_name: str, spec: CredentialSpec) -> str:
        if isinstance(spec, PlaintextCredential):
            return spec.password
        if isinstance(spec, EnvCredential):
            value = self._env.get(spec.variable)
            if value is None:
                raise CredentialResolutionError(
                    f"{account_name}: environment variable {spec.variable!r} is not set"
                )
            return value
        if isinstance(spec, CommandCredential):
            return _resolve_command(account_name, spec)
        # spec narrows to KeyringCredential by elimination on the union.
        raise CredentialResolutionError(
            f"{account_name}: the 'encrypted' credential backend "
            f"(service={spec.service}, username={spec.username}) "
            "is not available in v1. It requires the `keyring` package; "
            "approve adding it as a runtime dependency to enable this."
        )


def _resolve_command(account_name: str, spec: CommandCredential) -> str:
    try:
        completed = subprocess.run(
            list(spec.command),
            capture_output=True,
            check=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise CredentialResolutionError(
            f"{account_name}: credential command not found: {spec.command[0]!r}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CredentialResolutionError(
            f"{account_name}: credential command timed out after "
            f"{_COMMAND_TIMEOUT_SECONDS}s"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise CredentialResolutionError(
            f"{account_name}: credential command exited {exc.returncode}"
            + (f": {stderr}" if stderr else "")
        ) from exc
    return completed.stdout.rstrip("\n")


__all__ = [
    "CredentialResolutionError",
    "DefaultCredentialsProvider",
]
