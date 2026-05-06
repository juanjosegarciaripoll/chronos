from __future__ import annotations

import threading
from collections.abc import Callable

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from chronos.domain import OAuthCredential
from chronos.oauth import OAuthError, StoredTokens, run_loopback_flow


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
    ) -> None:
        super().__init__()
        self._account_name = account_name
        self._spec = spec
        self._on_complete = on_complete
        self._done = False
        self._lock = threading.Lock()

    def compose(self) -> ComposeResult:
        with Vertical(id="oauth-box", classes="dialog-box"):
            yield Label(
                f"Authorizing '{self._account_name}'",
                id="oauth-title",
                classes="dialog-title",
            )
            yield Label(
                "Opening browser — complete the Google sign-in and return here.",
                id="oauth-status",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="oauth-cancel", variant="warning")

    def on_mount(self) -> None:
        self._run_loopback()

    @work(thread=True)
    def _run_loopback(self) -> None:
        try:
            tokens = run_loopback_flow(
                client_id=self._spec.client_id,
                client_secret=self._spec.client_secret,
                scope=self._spec.scope,
            )
            self.app.call_from_thread(self._finish, tokens)  # pyright: ignore[reportUnknownMemberType]
        except OAuthError as exc:
            self.app.call_from_thread(self._finish, exc)  # pyright: ignore[reportUnknownMemberType]
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._finish, OAuthError(f"unexpected error: {exc}"))  # pyright: ignore[reportUnknownMemberType]

    def _finish(self, result: StoredTokens | BaseException) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        self._on_complete(result)
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "oauth-cancel":
            self.action_cancel_auth()

    def action_cancel_auth(self) -> None:
        self._finish(OAuthError("authorization cancelled"))


__all__ = ["OAuthProgressScreen"]
