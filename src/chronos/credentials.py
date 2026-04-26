from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from chronos.authorization import Authorization
from chronos.domain import (
    GOOGLE_OAUTH_SCOPE,
    AccountConfig,
    CommandCredential,
    CredentialSpec,
    EnvCredential,
    GoogleCredential,
    KeyringCredential,
    OAuthCredential,
    PlaintextCredential,
)
from chronos.oauth import OAuthError, StoredTokens, build_bearer_auth, save_tokens
from chronos.paths import oauth_token_path

_COMMAND_TIMEOUT_SECONDS = 30


# Returns fresh tokens for an account by running the OAuth loopback
# flow. Raises `OAuthError` if it can't (e.g. no browser available, or
# the TUI is owning the terminal).
InteractiveAuthorizer = Callable[[str, OAuthCredential, Path], StoredTokens]


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
      - OAuthCredential / GoogleCredential: load tokens from
        `paths.oauth_token_path(account)`. If the file is missing and
        an `interactive_authorizer` is configured, run it to obtain
        fresh tokens and save them. Otherwise raise.
    """

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        interactive_authorizer: InteractiveAuthorizer | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._authorizer = interactive_authorizer

    def build_auth(self, account: AccountConfig) -> Authorization:
        spec = account.credential
        if isinstance(spec, OAuthCredential):
            return _build_oauth_authorization(
                account.name, spec, interactive_authorizer=self._authorizer
            )
        if isinstance(spec, GoogleCredential):
            return _build_oauth_authorization(
                account.name,
                OAuthCredential(
                    client_id=spec.client_id,
                    client_secret=spec.client_secret,
                    scope=GOOGLE_OAUTH_SCOPE,
                    token_path=None,
                ),
                interactive_authorizer=self._authorizer,
            )
        password = self._resolve_password(account.name, spec)
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
        if isinstance(spec, KeyringCredential):
            raise CredentialResolutionError(
                f"{account_name}: the 'encrypted' credential backend "
                f"(service={spec.service}, username={spec.username}) "
                "is not available in v1. It requires the `keyring` "
                "package; approve adding it as a runtime dependency to "
                "enable this."
            )
        # spec narrows to OAuthCredential by elimination, but OAuth is
        # handled in build_auth() before this function is called.
        raise CredentialResolutionError(
            f"{account_name}: OAuth credentials must not be resolved as a "
            "password; this is an internal routing bug."
        )


def _build_oauth_authorization(
    account_name: str,
    spec: OAuthCredential,
    *,
    interactive_authorizer: InteractiveAuthorizer | None,
) -> Authorization:
    token_path = spec.token_path or oauth_token_path(account_name)
    if not token_path.exists():
        if interactive_authorizer is None:
            raise CredentialResolutionError(
                f"{account_name}: no stored OAuth tokens at {token_path}. "
                "Run `chronos sync` from an interactive terminal to authorize."
            )
        try:
            tokens = interactive_authorizer(account_name, spec, token_path)
        except OAuthError as exc:
            raise CredentialResolutionError(f"{account_name}: {exc}") from exc
        save_tokens(token_path, tokens)
    try:
        bearer = build_bearer_auth(
            client_id=spec.client_id,
            client_secret=spec.client_secret,
            scope=spec.scope,
            token_path=token_path,
        )
    except OAuthError as exc:
        raise CredentialResolutionError(f"{account_name}: {exc}") from exc
    return Authorization(http_auth=bearer, on_commit=bearer.persist)


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
    "InteractiveAuthorizer",
]
