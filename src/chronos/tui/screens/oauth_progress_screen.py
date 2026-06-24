from __future__ import annotations

import threading
from collections.abc import Callable

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from chronos.domain import OAuthCredential
from chronos.oauth import (
    OAuthError,
    StoredTokens,
    run_loopback_flow,
    run_paste_redirect_flow,
)


class OAuthProgressScreen(ModalScreen[None]):
    """Modal that runs the OAuth loopback flow and surfaces the result.

    The screen is pushed from the sync worker thread via
    `app.call_from_thread`. It runs the browser-redirect flow in its
    own worker, then calls `on_complete` with either `StoredTokens` (success)
    or a `BaseException` (failure/cancel) and pops itself.
    """

    BINDINGS = [Binding("escape", "cancel_auth", "Cancel", show=False)]

    def __init__(
        self,
        account_name: str,
        spec: OAuthCredential,
        *,
        on_complete: Callable[[StoredTokens | BaseException], None],
        remote_browser: bool = False,
    ) -> None:
        super().__init__()
        self._account_name = account_name
        self._spec = spec
        self._on_complete = on_complete
        self._remote_browser = remote_browser
        self._done = False
        self._lock = threading.Lock()
        self._callback_url: str | None = None
        self._callback_ready = threading.Event()

    def compose(self) -> ComposeResult:
        with Vertical(id="oauth-box", classes="dialog-box"):
            yield Label(
                f"Authorizing '{self._account_name}'",
                id="oauth-title",
                classes="dialog-title",
            )
            yield Label(
                (
                    "Open the URL below in your browser, then paste the final "
                    "redirected URL."
                    if self._remote_browser
                    else (
                        "Opening browser — complete the Google sign-in and return here."
                    )
                ),
                id="oauth-status",
            )
            if self._remote_browser:
                yield Label("", id="oauth-auth-url")
                yield Input(
                    placeholder="Paste redirected http://127.0.0.1 URL",
                    id="oauth-callback-url",
                )
            with Horizontal(classes="dialog-actions"):
                if self._remote_browser:
                    yield Button("Continue", id="oauth-submit", variant="primary")
                yield Button("Cancel", id="oauth-cancel", variant="warning")

    def on_mount(self) -> None:
        if self._remote_browser:
            self._run_remote_browser()
            callback_input: Input = self.query_one("#oauth-callback-url", Input)
            callback_input.focus()
        else:
            self._run_loopback()

    @work(thread=True)
    def _run_loopback(self) -> None:
        try:
            tokens = run_loopback_flow(
                client_id=self._spec.client_id,
                client_secret=self._spec.client_secret,
                scope=self._spec.scope,
            )
            self._call_from_thread(self._finish, tokens)
        except OAuthError as exc:
            self._call_from_thread(self._finish, exc)
        except Exception as exc:  # noqa: BLE001
            self._call_from_thread(
                self._finish,
                OAuthError(f"unexpected error: {exc}"),
            )

    @work(thread=True)
    def _run_remote_browser(self) -> None:
        try:
            tokens = run_paste_redirect_flow(
                client_id=self._spec.client_id,
                client_secret=self._spec.client_secret,
                scope=self._spec.scope,
                show_authorization_url=self._show_authorization_url,
                read_callback_url=self._read_callback_url,
            )
            self._call_from_thread(self._finish, tokens)
        except OAuthError as exc:
            self._call_from_thread(self._finish, exc)
        except Exception as exc:  # noqa: BLE001
            self._call_from_thread(
                self._finish,
                OAuthError(f"unexpected error: {exc}"),
            )

    def _show_authorization_url(self, auth_url: str, redirect_uri: str) -> None:
        self._call_from_thread(
            self._set_authorization_url,
            auth_url,
            redirect_uri,
        )

    def _call_from_thread(self, callback: Callable[..., object], *args: object) -> None:
        self.app.call_from_thread(  # pyright: ignore[reportUnknownMemberType]
            callback,
            *args,
        )

    def _set_authorization_url(self, auth_url: str, redirect_uri: str) -> None:
        label: Label = self.query_one("#oauth-auth-url", Label)
        label.update(
            "Authorize in your browser:\n"
            f"{auth_url}\n\n"
            "The final page may show a connection error. Copy its full address "
            f"from the browser bar. It should start with {redirect_uri}"
        )

    def _read_callback_url(self) -> str:
        self._callback_ready.wait()
        return self._callback_url or ""

    def _finish(self, result: StoredTokens | BaseException) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        self._callback_ready.set()
        self._on_complete(result)
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "oauth-cancel":
            self.action_cancel_auth()
        elif event.button.id == "oauth-submit":
            self._submit_callback_url()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "oauth-callback-url":
            self._submit_callback_url()

    def _submit_callback_url(self) -> None:
        if not self._remote_browser:
            return
        callback_input: Input = self.query_one("#oauth-callback-url", Input)
        value = callback_input.value.strip()
        if not value:
            status: Label = self.query_one("#oauth-status", Label)
            status.update("Paste the final redirected URL to continue.")
            return
        self._callback_url = value
        self._callback_ready.set()

    def action_cancel_auth(self) -> None:
        self._finish(OAuthError("authorization cancelled"))


__all__ = ["OAuthProgressScreen"]
