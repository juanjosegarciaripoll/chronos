"""Small auth-strategy type shared by the credentials layer and the
CalDAV HTTP session.

Exists because two subsystems need to exchange an "authenticated HTTP
strategy" without coupling to each other: `credentials.py` produces it,
`caldav/session.py` consumes it. Keeping it here avoids circular imports
and keeps `domain.py` free of HTTP-library types.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Authorization:
    """How to authenticate CalDAV requests for one account.

    Exactly one of `basic` or `bearer_token_fn` is populated.
    `bearer_token_fn`, if set, is called on every request to return a
    complete ``Authorization`` header value (e.g. ``"Bearer <token>"``).
    `on_commit`, if set, is called by the sync driver after a successful
    pass so implementations that rotate tokens (OAuth) can persist them.
    """

    basic: tuple[str, str] | None = None
    bearer_token_fn: Callable[[], str] | None = None
    on_commit: Callable[[], None] | None = None


__all__ = ["Authorization"]
