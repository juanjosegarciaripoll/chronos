"""Small auth-strategy type shared by the credentials layer and the
CalDAV HTTP session.

Exists because two subsystems need to exchange an "authenticated HTTP
strategy" without coupling to each other: `credentials.py` produces it,
`caldav_client.py` consumes it. Keeping it here avoids circular imports
and keeps `domain.py` free of HTTP-library types.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from niquests.auth import AuthBase


@dataclass(frozen=True)
class Authorization:
    """How to authenticate CalDAV requests for one account.

    Exactly one of `basic` or `http_auth` is populated. `on_commit`, if
    set, is called by the sync driver after a successful pass so
    implementations that rotate tokens (OAuth) can persist them.
    """

    basic: tuple[str, str] | None = None
    http_auth: AuthBase | None = None
    on_commit: Callable[[], None] | None = None


__all__ = ["Authorization"]
