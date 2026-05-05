"""Stdlib-only, connection-pooled HTTP client."""

from __future__ import annotations

import http.client
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from chronos.authorization import Authorization
from chronos.http.auth import apply_auth
from chronos.http.errors import HttpConnectionError, HttpStatusError

_MAX_REDIRECTS = 8


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class Client:
    """Connection-pooled HTTP client.

    Maintains one persistent connection per (scheme, host) pair.
    Supports auth, redirects, retry-on-disconnect, and custom HTTP
    methods (PROPFIND, REPORT, etc.).
    """

    def __init__(
        self,
        base_url: str,
        auth: Authorization | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._auth = auth
        self._timeout = timeout
        parsed = urlsplit(base_url)
        self._default_scheme = parsed.scheme or "https"
        self._default_netloc = parsed.netloc
        # Pool: (scheme, netloc) -> connection
        self._pool: dict[tuple[str, str], http.client.HTTPConnection] = {}
        self._ssl_ctx = ssl.create_default_context()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self, scheme: str, netloc: str) -> http.client.HTTPConnection:
        key = (scheme, netloc)
        conn = self._pool.get(key)
        if conn is None:
            conn = self._make_conn(scheme, netloc)
            self._pool[key] = conn
        return conn

    def _make_conn(
        self, scheme: str, netloc: str
    ) -> http.client.HTTPConnection:
        if scheme == "https":
            return http.client.HTTPSConnection(
                netloc, timeout=self._timeout, context=self._ssl_ctx
            )
        return http.client.HTTPConnection(netloc, timeout=self._timeout)

    def _replace_conn(self, scheme: str, netloc: str) -> http.client.HTTPConnection:
        key = (scheme, netloc)
        old = self._pool.pop(key, None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        conn = self._make_conn(scheme, netloc)
        self._pool[key] = conn
        return conn

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: Mapping[str, str] = {},
    ) -> HttpResponse:
        """Issue an HTTP request; follow redirects; raise on non-2xx."""
        scheme = self._default_scheme
        netloc = self._default_netloc
        current_path = path
        current_method = method
        current_body = body

        for redirect_count in range(_MAX_REDIRECTS + 1):
            req_headers: dict[str, str] = dict(headers)
            if self._auth is not None:
                apply_auth(req_headers, self._auth)
            if current_body:
                req_headers.setdefault("Content-Length", str(len(current_body)))

            resp = self._do_request(
                scheme,
                netloc,
                current_method,
                current_path,
                body=current_body,
                headers=req_headers,
            )

            # Redirect handling
            if resp.status in (301, 302, 307, 308):
                if redirect_count >= _MAX_REDIRECTS:
                    raise HttpStatusError(resp.status, resp.body, resp.headers)
                location = resp.headers.get("location", "")
                if not location:
                    raise HttpStatusError(resp.status, resp.body, resp.headers)
                parsed_loc = urlsplit(location)
                if parsed_loc.scheme and parsed_loc.netloc:
                    scheme = parsed_loc.scheme
                    netloc = parsed_loc.netloc
                    current_path = parsed_loc.path
                    if parsed_loc.query:
                        current_path += "?" + parsed_loc.query
                else:
                    current_path = location
                # Preserve method on 307/308; on 301/302 for non-GET, preserve too
                if resp.status in (307, 308):
                    pass  # keep method and body
                elif resp.status in (301, 302):
                    if current_method == "GET":
                        current_body = b""  # standard: GET stays GET, no body
                    # else: preserve method and body
                continue

            # Non-2xx raises
            if not (200 <= resp.status < 300):
                raise HttpStatusError(resp.status, resp.body, resp.headers)

            return resp

        raise HttpStatusError(resp.status, resp.body, resp.headers)  # type: ignore[possibly-undefined]

    def _do_request(
        self,
        scheme: str,
        netloc: str,
        method: str,
        path: str,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> HttpResponse:
        conn = self._get_conn(scheme, netloc)
        try:
            return self._send(conn, method, path, body=body, headers=headers)
        except (
            http.client.RemoteDisconnected,
            http.client.BadStatusLine,
            ConnectionResetError,
        ):
            # Retry once with a fresh connection
            conn = self._replace_conn(scheme, netloc)
            return self._send(conn, method, path, body=body, headers=headers)
        except (OSError, TimeoutError) as exc:
            raise HttpConnectionError(str(exc)) from exc

    def _send(
        self,
        conn: http.client.HTTPConnection,
        method: str,
        path: str,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> HttpResponse:
        try:
            conn.request(method, path, body=body or None, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
        except (OSError, TimeoutError) as exc:
            raise HttpConnectionError(str(exc)) from exc

        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return HttpResponse(
            status=resp.status,
            headers=resp_headers,
            body=resp_body,
        )

    def close(self) -> None:
        for conn in self._pool.values():
            try:
                conn.close()
            except Exception:
                pass
        self._pool.clear()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
