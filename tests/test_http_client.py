"""Unit tests for chronos.http.Client against a real ThreadingHTTPServer."""

from __future__ import annotations

import http.server
import threading
import unittest
from typing import Any

from chronos.authorization import Authorization
from chronos.http import Client, HttpResponse, HttpStatusError
from chronos.http.errors import HttpConnectionError


class _Handler(http.server.BaseHTTPRequestHandler):
    """Simple handler that dispatches to per-test logic via `server.handler_fn`."""

    def handle_request(self) -> None:
        fn = getattr(self.server, "handler_fn", None)
        if fn is not None:
            fn(self)
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

    def do_GET(self) -> None:  # noqa: N802
        self.handle_request()

    def do_PUT(self) -> None:  # noqa: N802
        self.handle_request()

    def do_PROPFIND(self) -> None:  # noqa: N802
        self.handle_request()

    def do_DELETE(self) -> None:  # noqa: N802
        self.handle_request()

    def do_REPORT(self) -> None:  # noqa: N802
        self.handle_request()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ARG002
        pass


class HttpClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self._thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self._thread.join(timeout=5)
        self.server.server_close()

    def _set_handler(self, fn: Any) -> None:
        self.server.handler_fn = fn  # type: ignore[attr-defined]

    def test_get_200_returns_http_response(self) -> None:
        def handler(req: _Handler) -> None:
            req.send_response(200)
            req.send_header("Content-Type", "text/plain")
            req.end_headers()
            req.wfile.write(b"hello")

        self._set_handler(handler)
        with Client(self.base_url) as client:
            resp = client.request("GET", "/")
        self.assertIsInstance(resp, HttpResponse)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"hello")

    def test_put_with_body_and_content_type(self) -> None:
        received: dict[str, Any] = {}

        def handler(req: _Handler) -> None:
            length = int(req.headers.get("Content-Length", 0))
            received["body"] = req.rfile.read(length)
            received["content_type"] = req.headers.get("Content-Type", "")
            req.send_response(201)
            req.end_headers()

        self._set_handler(handler)
        with Client(self.base_url) as client:
            resp = client.request(
                "PUT",
                "/resource",
                body=b"BEGIN:VCALENDAR",
                headers={"Content-Type": "text/calendar"},
            )
        self.assertEqual(resp.status, 201)
        self.assertEqual(received["body"], b"BEGIN:VCALENDAR")
        self.assertEqual(received["content_type"], "text/calendar")

    def test_propfind_custom_method(self) -> None:
        received: dict[str, Any] = {}

        def handler(req: _Handler) -> None:
            received["method"] = req.command
            req.send_response(207)
            req.end_headers()
            req.wfile.write(b"<multistatus/>")

        self._set_handler(handler)
        with Client(self.base_url) as client:
            resp = client.request("PROPFIND", "/cal/")
        self.assertEqual(resp.status, 207)
        self.assertEqual(received["method"], "PROPFIND")

    def test_401_raises_http_status_error(self) -> None:
        def handler(req: _Handler) -> None:
            req.send_response(401)
            req.send_header("WWW-Authenticate", 'Basic realm="test"')
            req.end_headers()

        self._set_handler(handler)
        with Client(self.base_url) as client:
            with self.assertRaises(HttpStatusError) as ctx:
                client.request("GET", "/protected")
        self.assertEqual(ctx.exception.status, 401)

    def test_basic_auth_header_is_set(self) -> None:
        import base64

        received: dict[str, Any] = {}

        def handler(req: _Handler) -> None:
            received["auth"] = req.headers.get("Authorization", "")
            req.send_response(200)
            req.end_headers()

        self._set_handler(handler)
        auth = Authorization(basic=("user", "pass"))
        with Client(self.base_url, auth=auth) as client:
            client.request("GET", "/")
        expected = (
            "Basic "
            + base64.b64encode(b"user:pass").decode()
        )
        self.assertEqual(received["auth"], expected)

    def test_301_redirect_followed(self) -> None:
        call_count = [0]

        def handler(req: _Handler) -> None:
            call_count[0] += 1
            if req.path == "/old":
                req.send_response(301)
                req.send_header("Location", f"http://127.0.0.1:{self.port}/new")
                req.end_headers()
            else:
                req.send_response(200)
                req.end_headers()
                req.wfile.write(b"new page")

        self._set_handler(handler)
        with Client(self.base_url) as client:
            resp = client.request("GET", "/old")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"new page")
        self.assertEqual(call_count[0], 2)

    def test_headers_are_lowercase(self) -> None:
        def handler(req: _Handler) -> None:
            req.send_response(200)
            req.send_header("X-Custom-Header", "value123")
            req.end_headers()

        self._set_handler(handler)
        with Client(self.base_url) as client:
            resp = client.request("GET", "/")
        # Headers should be lowercased
        self.assertIn("x-custom-header", resp.headers)
        self.assertEqual(resp.headers["x-custom-header"], "value123")

    def test_context_manager(self) -> None:
        def handler(req: _Handler) -> None:
            req.send_response(200)
            req.end_headers()

        self._set_handler(handler)
        with Client(self.base_url) as client:
            resp = client.request("GET", "/")
        self.assertEqual(resp.status, 200)
        # After __exit__, pool should be empty
        self.assertEqual(len(client._pool), 0)

    def test_bearer_auth_header_is_set(self) -> None:
        received: dict[str, Any] = {}

        def handler(req: _Handler) -> None:
            received["auth"] = req.headers.get("Authorization", "")
            req.send_response(200)
            req.end_headers()

        self._set_handler(handler)
        auth = Authorization(bearer_token_fn=lambda: "Bearer my-token")
        with Client(self.base_url, auth=auth) as client:
            client.request("GET", "/")
        self.assertEqual(received["auth"], "Bearer my-token")

    def test_500_raises_http_status_error(self) -> None:
        def handler(req: _Handler) -> None:
            req.send_response(500)
            req.end_headers()
            req.wfile.write(b"Internal Server Error")

        self._set_handler(handler)
        with Client(self.base_url) as client:
            with self.assertRaises(HttpStatusError) as ctx:
                client.request("GET", "/error")
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(ctx.exception.body, b"Internal Server Error")
