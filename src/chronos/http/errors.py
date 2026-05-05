from __future__ import annotations

from collections.abc import Mapping


class HttpError(Exception):
    pass


class HttpStatusError(HttpError):
    def __init__(self, status: int, body: bytes, headers: Mapping[str, str]) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body
        self.headers = dict(headers)


class HttpConnectionError(HttpError):
    pass
