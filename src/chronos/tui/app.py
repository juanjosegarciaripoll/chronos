from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from textual.app import App

from chronos.domain import AppConfig, SyncResult
from chronos.protocols import (
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from chronos.tui.screens.main_screen import MainScreen

SyncRunner = Callable[..., Sequence[SyncResult]]
"""Runs every configured account's sync.

Called as `runner()` for a one-shot run, or `runner(cancel_event=evt)`
to allow the caller (the TUI worker) to interrupt mid-flight. The
runtime accepts the kwarg unconditionally; the simple test fakes that
ignore it are valid implementations of the Protocol.
"""


@dataclass
class TuiServices:
    """Dependencies the TUI needs.

    Constructed by the CLI entry-point and handed to `ChronosApp`. Tests
    inject fakes for everything except `now`. `sync_runner` is `None`
    when the TUI was launched from a context that has not wired sync
    in (most TUI tests); pressing the sync key in that mode shows a
    notification rather than running anything.
    """

    config: AppConfig
    mirror: MirrorRepository
    index: IndexRepository
    creds: CredentialsProvider
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sync_runner: SyncRunner | None = None


class ChronosApp(App[None]):
    """Top-level Textual app.

    All real logic lives in `MainScreen`; the app is just a host. We
    push the main screen on mount instead of in `compose` so the
    constructor runs synchronously without touching any I/O.
    """

    # Textual binds Ctrl-P to its built-in command palette by default;
    # chronos's keyboard surface is small and visible from the F1 help
    # screen, so the palette is more confusing than useful here.
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    #main-body { height: 1fr; }
    CalendarPanel { width: 30; border-right: solid $accent; }
    #centre-pane { width: 1fr; }
    #view-title { padding: 0 1; color: $text-muted; }
    EventList { height: 2fr; }
    #detail-pane {
        height: 1fr;
        border-top: solid $accent;
        padding: 1;
    }
    #event-edit, #search-dialog, #confirm-box, #sync-confirm-box {
        padding: 1;
    }
    """

    def __init__(self, services: TuiServices) -> None:
        super().__init__()
        self.services = services

    def on_mount(self) -> None:
        self.push_screen(MainScreen())  # pyright: ignore[reportUnknownMemberType]


__all__ = ["ChronosApp", "SyncRunner", "TuiServices"]
