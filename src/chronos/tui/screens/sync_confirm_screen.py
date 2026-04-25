from __future__ import annotations

from collections.abc import Callable, Sequence

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Label

from chronos.domain import AccountConfig
from chronos.tui.bindings import confirm_bindings


class SyncConfirmScreen(Screen[None]):
    """Pre-sync confirmation: shows account list, asks before pushing.

    Per `CONVENTIONS.md §11`, the screen receives an `on_confirm`
    callback rather than a reference to `ChronosApp`. The actual sync
    runs from the callback the caller supplied (typically a method on
    `MainScreen`).
    """

    BINDINGS = confirm_bindings()

    def __init__(
        self,
        accounts: Sequence[AccountConfig],
        on_confirm: Callable[[], None],
    ) -> None:
        super().__init__()
        self._accounts = tuple(accounts)
        self._on_confirm = on_confirm

    def compose(self) -> ComposeResult:
        with Center(), Vertical(id="sync-confirm-box"):
            yield Label("Sync the following accounts?", id="sync-confirm-prompt")
            if not self._accounts:
                yield Label("(no accounts configured)", id="sync-confirm-empty")
            for account in self._accounts:
                yield Label(f" - {account.name} ({account.url})")
            yield Label("[y] Sync   [n] Cancel", id="sync-confirm-hint")
        yield Footer()

    def action_confirm(self) -> None:
        self._on_confirm()
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def action_cancel(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]


__all__ = ["SyncConfirmScreen"]
