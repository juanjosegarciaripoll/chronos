from __future__ import annotations

import base64

from chronos.authorization import Authorization


def apply_auth(headers: dict[str, str], auth: Authorization) -> None:
    """Apply authentication credentials to a request headers dict."""
    if auth.basic is not None:
        username, password = auth.basic
        cred = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"
    elif auth.bearer_token_fn is not None:
        headers["Authorization"] = auth.bearer_token_fn()
